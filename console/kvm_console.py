#!/usr/bin/env python3
"""Uno KVM console - the laptop side.

Shows the target's HDMI output (USB capture dongle, via OpenCV) in a window and
forwards this machine's keyboard + mouse to the target through a CH340 USB-TTL
adapter -> Arduino Uno R3 (kvm_bridge_328p + kvm_hid_16u2), which the target
sees as a plain USB keyboard and mouse. Works for BIOS, GRUB, installers.

Keys while the console is RELEASED (not capturing):
    Pause / Enter / click   capture keyboard + mouse (send them to the target)
    F                       toggle fullscreen
    Q / Esc                 quit
While CAPTURING everything goes to the target, except:
    Pause                   release capture (also happens on focus loss)

Usage:
    kvm_console.py                       auto-detect serial (CH340) + video 0
    kvm_console.py --serial /dev/ttyUSB0 --video /dev/video0 --fullscreen
    kvm_console.py --serial COM7 --video 1          (Windows laptop)
    kvm_console.py --list                           show serial ports and exit
    kvm_console.py --no-video                       input only (wiring test)
    kvm_console.py --bootloader                     reboot the 16U2 into HoodLoader2 for re-flashing
"""

import argparse
import builtins
import os
import sys
import threading
import time

os.environ.setdefault("SDL_HINT_GRAB_KEYBOARD", "1")   # X11: grab the keyboard too
# Windows: Media Foundation is the backend that actually delivers MJPG 1080p30 from an
# MS2109 stick (DirectShow silently falls back to raw YUY2 = 10 fps). With hardware
# transforms left on, MSMF takes ~30 s to open a device; this switches them off.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")


# Everything printed also lands in kvm_console.log next to the exe/script, flushed per line,
# so a frozen exe launched by double-click still leaves a readable trace.
def _open_log():
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    for path in (os.path.join(base, "kvm_console.log"), os.path.join(os.path.expanduser("~"), "kvm_console.log")):
        try:
            return open(path, "a", encoding="utf-8", buffering=1)
        except OSError:
            continue
    return None


_LOG = _open_log()
_print = builtins.print


def print(*args, **kwargs):  # noqa: A001
    kwargs.setdefault("flush", True)
    try:
        _print(*args, **kwargs)
    except OSError:
        pass
    if _LOG is not None:
        _LOG.write(time.strftime("%H:%M:%S ") + " ".join(str(a) for a in args) + "\n")


print("=== kvm_console start", sys.argv[1:])

import serial
import serial.tools.list_ports

# ---------------------------------------------------------------- protocol
SYNC = 0xA5
T_KEY_PRESS, T_KEY_RELEASE, T_KEY_RELEASE_ALL = 0x01, 0x02, 0x03
T_MOUSE_MOVE, T_MOUSE_BUTTONS = 0x10, 0x11
T_RELEASE_ALL, T_PING, T_PONG = 0x1F, 0x20, 0xA0
T_BOOTLOADER = 0x7E
BAUD = 38400
SC_PAUSE = 72          # SDL scancode == HID usage (page 7) for every key we forward


def frame(t, payload=b""):
    body = bytes([t, len(payload)]) + bytes(payload)
    crc = 0
    for b in body:
        crc ^= b
    return bytes([SYNC]) + body + bytes([crc])


def clamp8(v):
    return max(-127, min(127, int(v)))


# -------------------------------------------------------------------- link
class Link:
    """Serial link to the Arduino. Pings once a second; tracks the reply."""

    def __init__(self, port):
        self.port = port
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = BAUD
        self.ser.timeout = 0.05
        self.ser.dtr = False      # nothing is wired to DTR/RTS, but never pulse them
        self.ser.rts = False
        self.ser.open()
        self.lock = threading.Lock()
        self.last_pong = 0.0
        self.leds = 0
        self.banner = ""
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._pinger, daemon=True).start()

    def send(self, data):
        with self.lock:
            try:
                self.ser.write(data)
            except serial.SerialException:
                self.alive = False

    def ok(self):
        return (time.time() - self.last_pong) < 2.5

    def _pinger(self):
        while self.alive:
            self.send(frame(T_PING))
            time.sleep(1.0)

    def _reader(self):
        st, ftype, flen, buf, crc, text = 0, 0, 0, bytearray(), 0, bytearray()
        while self.alive:
            try:
                chunk = self.ser.read(64)
            except serial.SerialException:
                self.alive = False
                return
            for b in chunk:
                if st == 0:
                    if b == SYNC:
                        st = 1
                    elif b == 10:
                        self.banner = text.decode("ascii", "replace").strip()
                        text.clear()
                    elif 32 <= b < 127:
                        text.append(b)
                elif st == 1:
                    ftype, crc, st = b, b, 2
                elif st == 2:
                    flen, crc, buf, st = b, crc ^ b, bytearray(), (3 if b else 4)
                    if flen > 8:
                        st = 0
                elif st == 3:
                    buf.append(b)
                    crc ^= b
                    if len(buf) >= flen:
                        st = 4
                elif st == 4:
                    st = 0
                    if b == crc and ftype == T_PONG and flen == 1:
                        self.last_pong = time.time()
                        self.leds = buf[0]


def find_serial():
    ports = list(serial.tools.list_ports.comports())
    for p in ports:                       # CH340 first
        if p.vid == 0x1A86:
            return p.device
    for p in ports:
        if p.vid is not None and "Arduino" not in (p.description or ""):
            return p.device
    return None


def list_serial():
    for p in serial.tools.list_ports.comports():
        vid = f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None else "----:----"
        print(f"  {p.device:<14} {vid}  {p.description}")


def find_video(strict=False):
    """Locate the capture device. With strict=True, return None rather than falling back
    to any old camera, so callers can tell "the capture stick is attached" apart from
    "something with a lens exists". That distinction is what lets the console decide
    whether to start at all.

    Linux: first USB capture device (the dongle registers two nodes; index0 streams).
    Windows: probe cameras 0-4 and take the first that delivers a 1080p frame — the
    capture stick does, a laptop webcam usually does not."""
    if os.name != "nt":
        import glob
        import cv2
        # A console laptop usually has a built-in webcam too, and it can easily sort ahead
        # of the capture stick by name. Pick by capability instead: only the capture stick
        # delivers a full 1080-wide frame, so a webcam never wins by accident.
        nodes = sorted(glob.glob("/dev/v4l/by-id/*usb*video-index0")) or sorted(glob.glob("/dev/video*"))
        fallback = None
        for node in nodes:
            dev = os.path.realpath(node)
            cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap.release()
                continue
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
            ok, frm = cap.read()
            w = frm.shape[1] if ok and frm is not None else 0
            cap.release()
            print(f"video candidate {dev}: {('%dpx wide' % w) if ok else 'no frame'}")
            if ok and w >= 1900:
                return dev
            if ok and fallback is None:
                fallback = dev
        return None if strict else (fallback or "0")
    import cv2
    fallback = None
    for i in range(5):
        cap = cv2.VideoCapture(i, cv2.CAP_MSMF)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        ok, frm = cap.read()
        w = frm.shape[1] if ok and frm is not None else 0
        cap.release()
        print(f"camera {i}: {'%dpx wide' % w if ok else 'no frame'}")
        if ok and w >= 1900:
            return str(i)
        if ok and fallback is None:
            fallback = str(i)
    return None if strict else (fallback or "0")


def _fourcc_str(v):
    try:
        v = int(v)
        return "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4)) or "none"
    except Exception:  # noqa: BLE001
        return "?"


def probe_video(source):
    """Try the realistic backend/format/size combos and report what each really delivers.
    A USB-2.0 stick can only do 1080p at a usable rate as MJPG; raw YUY2 saturates the bus."""
    import cv2
    if source == "auto":
        source = find_video()
    src = int(source) if str(source).isdigit() else source
    backends = [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF)] if os.name == "nt" else [("V4L2", cv2.CAP_V4L2)]
    combos = [("MJPG", 1920, 1080), ("MJPG", 1280, 720), ("YUY2", 1280, 720), ("YUY2", 640, 480)]
    for bname, backend in backends:
        for fourcc, w, h in combos:
            cap = cv2.VideoCapture(src, backend)
            if not cap.isOpened():
                print(f"probe {bname} {fourcc} {w}x{h}: cannot open")
                cap.release()
                continue
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, 30)
            got = _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))
            ok, frm = cap.read()   # first frame is slow (pipeline start), don't time it
            n, t0, bright = 0, time.time(), 0.0
            while n < 40 and time.time() - t0 < 4.0:
                ok, frm = cap.read()
                if ok and frm is not None:
                    n += 1
                    bright = float(frm.mean())
            dt = max(time.time() - t0, 1e-6)
            shape = f"{frm.shape[1]}x{frm.shape[0]}" if ok and frm is not None else "no frame"
            print(f"probe {bname} {fourcc} {w}x{h}: got {got} {shape}, {n / dt:.1f} fps, brightness {bright:.0f}/255")
            cap.release()


def snapshot_video(source, path, width, height, fps, warmup=20):
    """Grab one frame and write it to a JPEG, with no window. Works over SSH on a
    machine whose desktop session is locked, which a pygame window cannot do."""
    import cv2
    v = Video(source, width, height, fps)
    frame, t0 = None, time.time()
    while time.time() - t0 < 15:
        with v.lock:
            if v.seq >= warmup and v.rgb is not None:
                frame = v.rgb.copy()
                break
        time.sleep(0.1)
    v.alive = False
    if frame is None:
        print("snapshot: no frames arrived")
        return 1
    bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    cv2.imwrite(path, bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(f"snapshot: wrote {path}, {bgr.shape[1]}x{bgr.shape[0]}, brightness {bgr.mean():.0f}/255")
    return 0


# ------------------------------------------------------------------- video
class Video:
    """Grabs frames from the capture dongle on a thread; keeps the latest RGB frame."""

    def __init__(self, source, width, height, fps):
        import cv2
        self.cv2 = cv2
        if source == "auto":
            source = find_video()
        src = int(source) if str(source).isdigit() else source
        backend = cv2.CAP_MSMF if os.name == "nt" else cv2.CAP_V4L2
        t0 = time.time()
        self.cap = cv2.VideoCapture(src, backend)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open video source {source!r}")
        print(f"video: backend {'MSMF' if os.name == 'nt' else 'V4L2'} opened in {time.time() - t0:.1f}s")
        # MJPG BEFORE the size, or a USB-2.0 stick falls back to raw YUY2 and 1080p
        # blows the bus budget (~10 fps). Force it, then confirm what actually stuck.
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        print(f"video: FOURCC now {_fourcc_str(self.cap.get(cv2.CAP_PROP_FOURCC))}")
        self._open_args = (src, backend, width, height, fps)
        self.lock = threading.Lock()
        self.rgb = None
        self.seq = 0
        self.last_frame = 0.0
        self.alive = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _reopen(self):
        """Re-acquire the capture device. The old handle never recovers once the device
        goes away, which happens every time the laptop suspends and resumes, and whenever
        the stick is unplugged and put back."""
        cv2 = self.cv2
        src, backend, w, h, fps = self._open_args
        try:
            self.cap.release()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
        cap = cv2.VideoCapture(src, backend)
        if not cap.isOpened():
            return False
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.cap = cap
        return True

    def _loop(self):
        cv2 = self.cv2
        fails = 0
        while self.alive:
            ok, bgr = self.cap.read()
            if not ok:
                fails += 1
                if fails in (1, 25):
                    print(f"video: read failed x{fails}")
                # About five seconds of unbroken failure means the device is gone rather
                # than glitching, so take a new handle instead of sitting on a dead one.
                if fails % 25 == 0:
                    print("video: reopening capture device")
                    if self._reopen():
                        print("video: reopened")
                        fails = 0
                time.sleep(0.2)
                continue
            fails = 0
            if self.seq == 0:
                print(f"video: first frame {bgr.shape[1]}x{bgr.shape[0]}, mean brightness {bgr.mean():.0f}/255")
                self._t_mark, self._seq_mark = time.time(), 0
            elif self.seq % 150 == 0 and self.seq <= 1800:
                # first minute only: real fps + brightness trace + a snapshot, for remote debugging
                now = time.time()
                fps_real = (self.seq - self._seq_mark) / max(now - self._t_mark, 1e-6)
                self._t_mark, self._seq_mark = now, self.seq
                print(f"video: frame {self.seq}, {fps_real:.1f} fps, mean brightness {bgr.mean():.0f}/255")
                try:
                    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
                    cv2.imwrite(os.path.join(base, "kvm_frame.jpg"), bgr, [cv2.IMWRITE_JPEG_QUALITY, 60])
                except Exception as e:  # noqa: BLE001
                    print(f"video: snapshot failed: {e}")
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            with self.lock:
                self.rgb = rgb
                self.seq += 1
                self.last_frame = time.time()

    def signal(self):
        return (time.time() - self.last_frame) < 1.5


# ----------------------------------------------------------------- console
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="serial port of the CH340 (auto-detect if omitted)")
    ap.add_argument("--video", default="auto",
                    help="capture device: 'auto' (default), an index (0, 1) or a path (/dev/video0)")
    ap.add_argument("--no-video", action="store_true", help="input only, no capture")
    ap.add_argument("--size", default="1920x1080", help="capture resolution, default 1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--bootloader", action="store_true",
                    help="tell the 16U2 to reboot into HoodLoader2 (for re-flashing) and exit")
    ap.add_argument("--probe", action="store_true",
                    help="measure what the capture device really delivers per backend/format/size, then exit")
    ap.add_argument("--snapshot", metavar="PATH",
                    help="save one captured frame to PATH as JPEG and exit, without opening a window")
    ap.add_argument("--check-devices", action="store_true",
                    help="exit 0 if both the serial adapter and a real capture device are attached, "
                         "1 otherwise; used to decide whether to start the console at boot")
    ap.add_argument("--wait", type=float, default=10.0, metavar="SECONDS",
                    help="with --check-devices, keep looking this long before giving up "
                         "(default 10), so a cold boot does not lose the race with USB enumeration")
    args = ap.parse_args()

    if args.list:
        list_serial()
        return 0
    if args.probe:
        probe_video(args.video)
        return 0
    if args.check_devices:
        # A cold boot reaches the login shell in seconds, which can beat udev finishing
        # USB enumeration. Looking once would drop to a shell with the hardware about to
        # appear, so keep asking until the deadline, re-probing only what is still missing.
        deadline = time.time() + args.wait
        port = cap = None
        while True:
            if port is None:
                port = args.serial or find_serial()
            if cap is None:
                cap = find_video(strict=True) if args.video == "auto" else args.video
            if (port and cap) or time.time() >= deadline:
                break
            time.sleep(1.0)
        print(f"serial adapter: {port or 'not attached'}")
        print(f"capture device: {cap or 'not attached'}")
        return 0 if (port and cap) else 1
    if args.snapshot:
        sw, sh = (int(x) for x in args.size.lower().split("x"))
        return snapshot_video(args.video, args.snapshot, sw, sh, args.fps)

    port = args.serial or find_serial()
    if not port:
        print("No CH340 serial port found. Ports seen:")
        list_serial()
        return 1
    link = Link(port)
    print(f"serial: {port} @ {BAUD}")

    if args.bootloader:
        time.sleep(1.5)
        print("HID link:", "ok" if link.ok() else "no reply (sending anyway)")
        link.send(frame(T_BOOTLOADER, b"BL"))
        print("sent bootloader command; the target should now see a 'HoodLoader2 Uno' serial port")
        return 0

    video = None
    if not args.no_video:
        w, h = (int(x) for x in args.size.lower().split("x"))
        try:
            video = Video(args.video, w, h, args.fps)
            print(f"video: source={args.video} -> opened, asked {w}x{h}@{args.fps}, "
                  f"got {int(video.cap.get(3))}x{int(video.cap.get(4))}@{video.cap.get(5):.0f}")
        except Exception as e:  # noqa: BLE001
            print(f"video disabled: {e}")

    import pygame
    pygame.init()
    pygame.display.set_caption("Uno KVM")
    flags = pygame.RESIZABLE
    if args.fullscreen:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    else:
        screen = pygame.display.set_mode((1280, 720), flags)
    font = pygame.font.SysFont("monospace", 16)
    clock = pygame.time.Clock()
    focus_lost_event = getattr(pygame, "WINDOWFOCUSLOST", None)

    captured = False
    fullscreen = args.fullscreen
    buttons = 0
    acc_x = acc_y = acc_w = 0
    last_flush = 0.0
    surf_cache = (None, -1)

    def set_capture(on):
        nonlocal captured, buttons, acc_x, acc_y, acc_w
        captured = on
        pygame.event.set_grab(on)
        pygame.mouse.set_visible(not on)
        if hasattr(pygame.mouse, "set_relative_mode"):
            try:
                pygame.mouse.set_relative_mode(on)
            except Exception:  # noqa: BLE001
                pass
        if not on:
            link.send(frame(T_RELEASE_ALL))
            buttons = 0
            acc_x = acc_y = acc_w = 0
        pygame.event.clear()

    def toggle_fullscreen():
        nonlocal screen, fullscreen
        fullscreen = not fullscreen
        if fullscreen:
            screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            screen = pygame.display.set_mode((1280, 720), flags)

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif focus_lost_event is not None and ev.type == focus_lost_event:
                if captured:
                    set_capture(False)
            elif ev.type == pygame.KEYDOWN or ev.type == pygame.KEYUP:
                sc = getattr(ev, "scancode", 0)
                down = ev.type == pygame.KEYDOWN
                if captured:
                    if sc == SC_PAUSE:
                        if down:
                            set_capture(False)
                    elif 4 <= sc <= 0xE7:
                        link.send(frame(T_KEY_PRESS if down else T_KEY_RELEASE, bytes([sc])))
                elif down:
                    if sc == SC_PAUSE or ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        set_capture(True)
                    elif ev.key == pygame.K_f:
                        toggle_fullscreen()
                    elif ev.key in (pygame.K_q, pygame.K_ESCAPE):
                        running = False
            elif captured and ev.type == pygame.MOUSEMOTION:
                acc_x += ev.rel[0]
                acc_y += ev.rel[1]
            elif captured and ev.type == pygame.MOUSEWHEEL:
                acc_w += ev.y
            elif captured and ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                bit = {1: 1, 3: 2, 2: 4}.get(ev.button)     # pygame: 1 left, 2 middle, 3 right
                if bit:
                    if ev.type == pygame.MOUSEBUTTONDOWN:
                        buttons |= bit
                    else:
                        buttons &= ~bit
                    link.send(frame(T_MOUSE_BUTTONS, bytes([buttons])))
            elif not captured and ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                set_capture(True)

        now = time.time()
        if captured and (acc_x or acc_y or acc_w) and (now - last_flush) >= 0.008:
            while acc_x or acc_y or acc_w:
                dx, dy, dw = clamp8(acc_x), clamp8(acc_y), clamp8(acc_w)
                link.send(frame(T_MOUSE_MOVE, bytes([dx & 0xFF, dy & 0xFF, dw & 0xFF])))
                acc_x -= dx
                acc_y -= dy
                acc_w -= dw
            last_flush = now

        # ---- draw
        screen.fill((0, 0, 0))
        sw, sh = screen.get_size()
        if video is not None:
            with video.lock:
                rgb, seq = video.rgb, video.seq
            if rgb is not None:
                if surf_cache[1] != seq:
                    h, w = rgb.shape[:2]
                    surf = pygame.image.frombuffer(rgb.tobytes(), (w, h), "RGB")
                    scale = min(sw / w, sh / h)
                    tw, th = int(w * scale), int(h * scale)
                    if (tw, th) != (w, h):
                        # smoothscale, not scale: the console is usually larger than this
                        # panel, and plain scale point-samples, so shrinking 1920 onto 1366
                        # silently drops about three of every ten rows and columns. Console
                        # glyphs have single-pixel strokes, so those strokes vanish outright
                        # and text turns to mush. Area-averaging keeps them as grey instead.
                        surf = pygame.transform.smoothscale(surf, (tw, th))
                    surf_cache = (surf, seq)
                surf = surf_cache[0]
                screen.blit(surf, ((sw - surf.get_width()) // 2, (sh - surf.get_height()) // 2))
            if not video.signal():
                msg = font.render("NO VIDEO SIGNAL", True, (255, 80, 80))
                screen.blit(msg, ((sw - msg.get_width()) // 2, sh // 2))

        hid = "HID ok" if link.ok() else ("HID NO REPLY" if link.alive else "SERIAL LOST")
        leds = "".join(n for n, bit in (("NUM ", 1), ("CAPS ", 2), ("SCRL ", 4)) if link.leds & bit)
        if captured:
            status = f"CAPTURING  |  Pause = release  |  {hid} {leds}"
            color = (120, 220, 120) if link.ok() else (255, 120, 80)
        else:
            status = (f"RELEASED  |  Pause/Enter/click = capture   F = fullscreen   "
                      f"Ctrl+Alt+F2 = shell   Q = quit  |  {hid}")
            color = (230, 200, 90)
        bar = font.render(status, True, color)
        screen.blit(bar, (8, sh - bar.get_height() - 6))
        pygame.display.flip()
        clock.tick(60)

    set_capture(False)
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
