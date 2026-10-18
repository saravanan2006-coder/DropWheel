#!/usr/bin/env python3
"""DropWheel - drag a file, press the hotkey, drop it on a wheel segment.

Radial drop-to-convert overlay for KDE Plasma (Wayland or X11).
Usage:
  dropwheel --daemon        start in background (optional, autostart)
  dropwheel --show          show the wheel (bind this to a global shortcut)
  dropwheel FILE...         show the wheel with files preloaded (click a segment)
"""
import sys, os, json, math, shutil, subprocess, tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QPointF, QRectF, QThread, Signal, QVariantAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPainterPath, QRadialGradient, QLinearGradient, QFontMetrics, QCursor
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SOCK = "dropwheel-" + str(os.getuid())


# ----------------------------------------------------------------- actions
@dataclass
class Action:
    label: str
    ext: Optional[str]            # None = keep the source extension
    build: Callable               # (src: Path, out: Path) -> argv list
    suffix: str = ""


def magick(*a):  return lambda s, o: ["magick", f"{s}[0]", *a, str(o)]
def ffmpeg(*a):  return lambda s, o: ["ffmpeg", "-y", "-loglevel", "error", "-i", str(s), *a, str(o)]
def pdfimg(fmt): return lambda s, o: ["pdftoppm", f"-{fmt}", "-r", "150", str(s), str(o.with_suffix(""))]
def gs_small(s, o): return ["gs", "-sDEVICE=pdfwrite", "-dPDFSETTINGS=/ebook", "-dNOPAUSE", "-dBATCH", "-dQUIET", f"-sOutputFile={o}", str(s)]
def pdf_txt(s, o):  return ["pdftotext", "-layout", str(s), str(o)]


def doc(ext, flt=None):
    def b(s, o):
        if ext in ("md", "html") or s.suffix.lower() in (".md", ".epub"):
            fmt = {"md": ["-t", "gfm", "--wrap=none"], "html": ["-s"], "txt": ["-t", "plain"]}.get(ext, [])
            return ["pandoc", str(s), *fmt, "-o", str(o)]
        return ["soffice", "-env:UserInstallation=file:///tmp/dropwheel-lo", "--headless",
                "--convert-to", flt or ext, "--outdir", str(o.parent), str(s)]
    return b


MP4 = ("-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart")
ACTIONS = {
    "IMAGE": [Action("PNG", "png", magick()),
              Action("JPG", "jpg", magick("-background", "white", "-flatten", "-quality", "92")),
              Action("WEBP", "webp", magick("-quality", "88")),
              Action("AVIF", "avif", magick("-quality", "60")),
              Action("PDF", "pdf", magick()),
              Action("ICON", "ico", magick("-resize", "256x256>")),
              Action("COMPRESS", None, magick("-strip", "-quality", "72"), " compressed"),
              Action("STRIP\nMETA", None, magick("-strip"), " clean")],
    "VIDEO": [Action("MP4", "mp4", ffmpeg("-crf", "22", *MP4)),
              Action("WEBM", "webm", ffmpeg("-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-c:a", "libopus")),
              Action("GIF", "gif", ffmpeg("-vf", "fps=12,scale=480:-1:flags=lanczos", "-loop", "0")),
              Action("MKV", "mkv", ffmpeg("-c", "copy")),
              Action("AUDIO\nMP3", "mp3", ffmpeg("-vn", "-c:a", "libmp3lame", "-q:a", "2")),
              Action("COMPRESS", "mp4", ffmpeg("-crf", "28", *MP4), " compressed")],
    "AUDIO": [Action("MP3", "mp3", ffmpeg("-vn", "-c:a", "libmp3lame", "-q:a", "2")),
              Action("FLAC", "flac", ffmpeg("-vn", "-c:a", "flac")),
              Action("WAV", "wav", ffmpeg("-vn")),
              Action("OGG", "ogg", ffmpeg("-vn", "-c:a", "libvorbis", "-q:a", "5")),
              Action("OPUS", "opus", ffmpeg("-vn", "-c:a", "libopus", "-b:a", "128k")),
              Action("M4A", "m4a", ffmpeg("-vn", "-c:a", "aac", "-b:a", "192k"))],
    "PDF":   [Action("PNG\nPAGES", "png", pdfimg("png")),
              Action("JPG\nPAGES", "jpg", pdfimg("jpeg")),
              Action("TEXT", "txt", pdf_txt),
              Action("COMPRESS", None, gs_small, " compressed")],
    "DOC":   [Action("PDF", "pdf", doc("pdf")),
              Action("DOCX", "docx", doc("docx")),
              Action("ODT", "odt", doc("odt")),
              Action("TXT", "txt", doc("txt", "txt:Text")),
              Action("HTML", "html", doc("html")),
              Action("MD", "md", doc("md"))],
}
CATS = {
    "IMAGE": {"png", "jpg", "jpeg", "webp", "avif", "bmp", "tif", "tiff", "gif", "heic", "svg", "ico"},
    "VIDEO": {"mp4", "mkv", "webm", "mov", "avi", "flv", "m4v"},
    "AUDIO": {"mp3", "flac", "wav", "ogg", "opus", "m4a", "aac"},
    "PDF":   {"pdf"},
    "DOC":   {"docx", "doc", "odt", "rtf", "txt", "md", "html", "htm", "epub"},
}


def category(p: Path):
    e = p.suffix.lower().lstrip(".")
    return next((k for k, v in CATS.items() if e in v), None)


def unique(p: Path) -> Path:
    i = 1
    while p.exists():
        p = p.with_name(f"{p.stem.rsplit(' (', 1)[0]} ({i}){p.suffix}")
        i += 1
    return p


def notify(title, body, ok=True):
    try:
        subprocess.Popen(["notify-send", "-a", "DropWheel", "-i",
                          "emblem-ok-symbolic" if ok else "dialog-warning", title, body])
    except OSError:
        pass


class Worker(QThread):
    step = Signal(str)
    done = Signal(list, list)

    def __init__(self, files, action):
        super().__init__()
        self.files, self.a = files, action

    def run(self):
        outs, errs = [], []
        for n, src in enumerate(self.files, 1):
            self.step.emit(f"{n}/{len(self.files)}  {src.name}")
            ext = (self.a.ext or src.suffix.lstrip(".")).lower()
            if ext == src.suffix.lstrip(".").lower() and not self.a.suffix:
                errs.append(f"{src.name} is already .{ext}")
                continue
            with tempfile.TemporaryDirectory(prefix="dropwheel-") as td:
                out = Path(td) / f"{src.stem}{self.a.suffix}.{ext}"
                try:
                    r = subprocess.run(self.a.build(src, out), capture_output=True, text=True, timeout=3600)
                except FileNotFoundError as e:
                    errs.append(f"missing tool: {e.filename}")
                    continue
                except subprocess.TimeoutExpired:
                    errs.append(f"{src.name}: timed out")
                    continue
                if r.returncode:
                    errs.append(f"{src.name}: {(r.stderr or r.stdout).strip()[-140:]}")
                    continue
                for f in sorted(Path(td).iterdir()):
                    dest = unique(src.parent / f.name)
                    shutil.move(str(f), dest)
                    outs.append(dest)
        self.done.emit(outs, errs)


# -------------------------------------------------------------------- UI
def sector(c, r1, r2, start, span):
    o = QRectF(c.x() - r2, c.y() - r2, 2 * r2, 2 * r2)
    i = QRectF(c.x() - r1, c.y() - r1, 2 * r1, 2 * r1)
    p = QPainterPath()
    p.arcMoveTo(o, -start)
    p.arcTo(o, -start, -span)
    p.arcTo(i, -(start + span), span)
    p.closeSubpath()
    return p


class Wheel(QWidget):
    R_IN, R_OUT, R_HUB = 78, 184, 68

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.files, self.cat, self.hover = [], None, -1
        self.state, self.msg, self.spin, self.t = "idle", "", 0, 0.0
        self.center, self.anchored, self.workers = QPointF(400, 300), False, []
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(240)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._tick)
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._spin)
        self.f_label = QFont(); self.f_label.setPointSizeF(8.5); self.f_label.setBold(True)
        self.f_label.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        self.f_hub = QFont(); self.f_hub.setPointSizeF(9); self.f_hub.setBold(True)
        self.f_sub = QFont(); self.f_sub.setPointSizeF(7.5)

    # -- state helpers
    @property
    def actions(self):
        return ACTIONS.get(self.cat, [])

    def set_files(self, files):
        paths = [Path(f) for f in files or [] if os.path.isfile(f)]
        if not paths:
            return
        cat = category(paths[0])
        self.cat = cat
        self.files = [p for p in paths if category(p) == cat]

    def present(self, files=None):
        if self.isVisible() and self.state == "idle":
            return self.dismiss()
        self.files, self.cat, self.hover, self.state = [], None, -1, "idle"
        self.set_files(files)
        self.anchored = bool(self.files)
        scr = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        self.setScreen(scr)
        g = scr.geometry()
        self.center = QPointF(g.width() / 2, g.height() / 2)
        self.showFullScreen()
        self.raise_(); self.activateWindow()
        self.anim.stop(); self.anim.start()

    def dismiss(self):
        self.timer.stop()
        self.hide()

    def _tick(self, v):
        self.t = float(v); self.update()

    def _spin(self):
        self.spin = (self.spin + 7) % 360; self.update()

    def hit(self, pos):
        n = len(self.actions)
        if not n:
            return -1
        d = pos - self.center
        r = math.hypot(d.x(), d.y())
        if r < self.R_IN or r > self.R_OUT + 12:
            return -1
        span = 360 / n
        ang = math.degrees(math.atan2(d.y(), d.x()))
        return int(((ang + 90 + span / 2) % 360) // span)

    def far(self, pos):
        d = pos - self.center
        return math.hypot(d.x(), d.y()) > self.R_OUT + 30

    # -- running an action
    def run(self, idx):
        act = self.actions[idx]
        self.state, self.msg = "busy", act.label.replace("\n", " ")
        w = Worker(list(self.files), act)
        self.workers.append(w)
        w.step.connect(lambda m: setattr(self, "msg", m))
        w.done.connect(self._done)
        w.finished.connect(lambda w=w: self.workers.remove(w))
        w.start()
        self.timer.start()
        self.update()

    def _done(self, outs, errs):
        self.timer.stop()
        self.state = "ok" if outs else "fail"
        self.msg = f"{len(outs)} file(s) saved" if outs else (errs[0] if errs else "nothing done")
        if outs:
            notify("DropWheel: done", ", ".join(p.name for p in outs[:3]) + ("…" if len(outs) > 3 else ""))
        if errs:
            notify("DropWheel: problem", "\n".join(errs[:3]), ok=False)
        self.update()
        QTimer.singleShot(1300 if outs and not errs else 2800, self.dismiss)

    # -- events
    def dragEnterEvent(self, e):
        if self.state != "idle" or not e.mimeData().hasUrls():
            return e.ignore()
        self.set_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])
        if not self.anchored:                       # wheel opens right under the cursor
            m = self.R_OUT + 30
            p = e.position()
            self.center = QPointF(min(max(p.x(), m), self.width() - m), min(max(p.y(), m), self.height() - m))
            self.anchored = True
        e.setDropAction(Qt.CopyAction); e.accept(); self.update()

    def dragMoveEvent(self, e):
        self.hover = self.hit(e.position())
        e.setDropAction(Qt.CopyAction); e.accept(); self.update()

    def dropEvent(self, e):
        e.setDropAction(Qt.CopyAction); e.accept()
        idx = self.hit(e.position())
        if idx >= 0 and self.files:
            self.run(idx)
        elif self.far(e.position()):
            self.dismiss()

    def mouseMoveEvent(self, e):
        self.hover = self.hit(e.position()); self.update()

    def mousePressEvent(self, e):
        if self.state != "idle":
            return
        idx = self.hit(e.position())
        if idx >= 0 and self.files:
            self.run(idx)
        elif self.far(e.position()):
            self.dismiss()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.dismiss()

    # -- drawing
    def hub_text(self):
        if self.state != "idle":
            return {"busy": "Working…", "ok": "✓ Done", "fail": "✕ Failed"}[self.state], self.msg
        if not self.files:
            return "Drop files", "on a segment"
        if not self.actions:
            return "Unsupported", self.files[0].suffix or "file type"
        more = f"  +{len(self.files) - 1}" if len(self.files) > 1 else ""
        return self.cat + more, self.files[0].name

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t, c = self.t, self.center
        p.fillRect(self.rect(), QColor(16, 8, 30, int(120 * t)))
        p.setOpacity(t)
        p.translate(c); p.scale(.88 + .12 * t, .88 + .12 * t); p.translate(-c)
        acts = self.actions
        n = len(acts) or 8
        span, gap = 360 / n, 2.4
        busy = self.state != "idle"

        glow = QRadialGradient(c, self.R_OUT + 50)
        glow.setColorAt(.6, QColor(255, 120, 70, 80)); glow.setColorAt(1, QColor(255, 120, 70, 0))
        p.setPen(Qt.NoPen); p.setBrush(glow)
        p.drawEllipse(c, self.R_OUT + 50, self.R_OUT + 50)
        p.setBrush(QColor(255, 236, 220, 70)); p.setPen(QPen(QColor(255, 255, 255, 110), 1.5))
        p.drawEllipse(c, self.R_OUT + 10, self.R_OUT + 10)

        rm = (self.R_IN + self.R_OUT) / 2
        for i in range(n):
            start = -90 - span / 2 + i * span + gap / 2
            hot = acts and i == self.hover and not busy
            if hot:
                g = QLinearGradient(c.x(), c.y() - self.R_OUT, c.x(), c.y() + self.R_OUT)
                g.setColorAt(0, QColor(255, 140, 86, 245)); g.setColorAt(1, QColor(255, 92, 64, 245))
                p.setBrush(g)
            else:
                p.setBrush(QColor(255, 228, 208, 175 if acts and not busy else 55))
            p.setPen(QPen(QColor(255, 255, 255, 160 if acts else 60), 1.4))
            p.drawPath(sector(c, self.R_IN, self.R_OUT, start, span - gap))
            if i < len(acts):
                mid = math.radians(start + (span - gap) / 2)
                q = QPointF(c.x() + rm * math.cos(mid), c.y() + rm * math.sin(mid))
                p.setFont(self.f_label)
                p.setPen(QColor(255, 255, 255) if hot else QColor(96, 44, 28, 110 if busy else 255))
                p.drawText(QRectF(q.x() - 52, q.y() - 26, 104, 52), Qt.AlignCenter | Qt.TextWordWrap, acts[i].label)

        # hub
        p.setBrush(QColor(255, 246, 238, 238)); p.setPen(QPen(QColor(255, 255, 255, 220), 2))
        p.drawEllipse(c, self.R_HUB, self.R_HUB)
        title, sub = self.hub_text()
        w = 2 * self.R_HUB - 26
        if self.state == "busy":
            p.setPen(QPen(QColor(255, 106, 61), 4, Qt.SolidLine, Qt.RoundCap))
            r = QRectF(c.x() - self.R_HUB + 6, c.y() - self.R_HUB + 6, 2 * self.R_HUB - 12, 2 * self.R_HUB - 12)
            p.drawArc(r, int(-self.spin * 16), 100 * 16)
        p.setPen(QColor(96, 44, 28))
        p.setFont(self.f_hub)
        p.drawText(QRectF(c.x() - w / 2, c.y() - 22, w, 22), Qt.AlignCenter,
                   QFontMetrics(self.f_hub).elidedText(title, Qt.ElideRight, int(w)))
        p.setFont(self.f_sub); p.setPen(QColor(140, 84, 62))
        p.drawText(QRectF(c.x() - w / 2, c.y() + 2, w, 20), Qt.AlignCenter,
                   QFontMetrics(self.f_sub).elidedText(sub, Qt.ElideMiddle, int(w)))


# ------------------------------------------------------------------ main
def send(payload) -> bool:
    s = QLocalSocket()
    s.connectToServer(SOCK)
    if not s.waitForConnected(200):
        return False
    s.write(json.dumps(payload).encode())
    s.waitForBytesWritten(500)
    s.disconnectFromServer()
    return True


def main():
    args = sys.argv[1:]
    files = [str(Path(a).resolve()) for a in args if not a.startswith("--")]
    payload = {"show": "--show" in args or bool(files), "files": files}
    app = QApplication(sys.argv)
    if send(payload):
        return 0
    app.setQuitOnLastWindowClosed(False)
    app.setDesktopFileName("dropwheel")
    wheel = Wheel()
    QLocalServer.removeServer(SOCK)
    server = QLocalServer()
    server.listen(SOCK)

    def on_conn():
        s = server.nextPendingConnection()

        def rd():
            try:
                d = json.loads(bytes(s.readAll()).decode())
            except ValueError:
                return
            if d.get("show"):
                wheel.present(d.get("files"))
        s.readyRead.connect(rd)

    server.newConnection.connect(on_conn)
    if payload["show"]:
        wheel.present(files)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
