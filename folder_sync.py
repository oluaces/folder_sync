#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FolderSync - Sincronizador de carpetas con informe previo (PySide6)

Requisitos:
    pip install PySide6

Uso:
    python folder_sync.py

Características:
  * Drag & drop de carpetas de origen y destino (o botón «Examinar…»).
  * Modo unidireccional (origen → destino) o bidireccional (gana el más reciente).
  * Criterios de comparación combinables: fecha de modificación (con tolerancia),
    tamaño y contenido (hash).
  * Política de sobrescritura, eliminación de sobrantes en destino (modo espejo),
    envío a la papelera, subcarpetas y patrones a ignorar.
  * Informe detallado ANTES de sincronizar: cada operación se puede marcar o
    desmarcar individualmente, filtrar por tipo y exportar a un fichero de texto.
  * El análisis y la sincronización se ejecutan en hilos y se pueden cancelar.
  * Las copias se hacen a un fichero temporal y se renombran al terminar, de modo
    que un fallo a mitad de copia nunca deja un fichero de destino corrupto.
"""
from __future__ import annotations

import fnmatch
import hashlib
import math
import os
import shutil
import stat
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QFile, QPointF, QRectF, QSettings, QThread, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ============================================================================
#  MODELO
# ============================================================================


class Action(Enum):
    CREATE_DIR = "Crear carpeta"
    COPY = "Copiar"
    OVERWRITE = "Sobrescribir"
    DELETE_FILE = "Eliminar archivo"
    DELETE_DIR = "Eliminar carpeta"
    CONFLICT = "Conflicto"
    SKIP = "Omitido"


ACTION_ORDER = list(Action)
ACTIONABLE = {
    Action.CREATE_DIR,
    Action.COPY,
    Action.OVERWRITE,
    Action.DELETE_FILE,
    Action.DELETE_DIR,
}
ACTION_ICON = {
    Action.CREATE_DIR: "▣",
    Action.COPY: "＋",
    Action.OVERWRITE: "↻",
    Action.DELETE_FILE: "✖",
    Action.DELETE_DIR: "✖",
    Action.CONFLICT: "⚠",
    Action.SKIP: "–",
}
ACTION_COLOR = {
    Action.CREATE_DIR: QColor(70, 130, 230, 55),
    Action.COPY: QColor(60, 170, 90, 55),
    Action.OVERWRITE: QColor(240, 160, 40, 70),
    Action.DELETE_FILE: QColor(220, 60, 60, 60),
    Action.DELETE_DIR: QColor(220, 60, 60, 60),
    Action.CONFLICT: QColor(170, 80, 200, 60),
    Action.SKIP: QColor(128, 128, 128, 40),
}

POLICY_DIFFERENT = "different"
POLICY_NEWER = "newer"
POLICY_NEVER = "never"

DIR_ORIGIN_TO_DEST = "Origen → Destino"
DIR_DEST_TO_ORIGIN = "Destino → Origen"
DIR_DELETE_DEST = "Eliminar en destino"


class Cancelled(Exception):
    """Se lanza cuando el usuario cancela la operación."""


@dataclass
class FileInfo:
    rel: str  # ruta relativa con «/» como separador
    path: str  # ruta absoluta real
    is_dir: bool
    size: int = 0
    mtime: float = 0.0


@dataclass
class SyncOptions:
    origin: str
    dest: str
    two_way: bool = False
    recursive: bool = True
    cmp_mtime: bool = True
    cmp_size: bool = True
    cmp_hash: bool = False
    tolerance: float = 2.0
    ignore_tz_shift: bool = True
    overwrite_policy: str = POLICY_DIFFERENT
    delete_extra: bool = False
    use_trash: bool = True
    ignore: List[str] = field(default_factory=list)


@dataclass
class PlanItem:
    action: Action
    rel: str
    reason: str = ""
    direction: str = ""
    a_info: Optional[FileInfo] = None  # estado en la carpeta de origen
    b_info: Optional[FileInfo] = None  # estado en la carpeta de destino
    from_path: str = ""
    to_path: str = ""
    size: int = 0  # bytes a transferir
    checked: bool = True

    @property
    def actionable(self) -> bool:
        return self.action in ACTIONABLE

    @property
    def is_dir(self) -> bool:
        info = self.a_info or self.b_info
        return bool(info and info.is_dir)


@dataclass
class Plan:
    options: SyncOptions
    items: List[PlanItem] = field(default_factory=list)
    identical: int = 0
    kept_extra: int = 0  # elementos solo en destino que se conservan


@dataclass
class SyncResult:
    done: Counter = field(default_factory=Counter)
    errors: List[str] = field(default_factory=list)
    bytes_copied: int = 0
    skipped_nonempty: int = 0
    cancelled: bool = False


# ============================================================================
#  UTILIDADES
# ============================================================================


def fmt_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "?"


def join_rel(root: str, rel: str) -> str:
    return os.path.join(root, *rel.split("/"))


def is_nested_or_same(a: str, b: str) -> bool:
    a = os.path.normcase(os.path.realpath(a))
    b = os.path.normcase(os.path.realpath(b))
    try:
        return os.path.commonpath([a, b]) in (a, b)
    except ValueError:  # unidades distintas en Windows
        return False


def is_ignored(name: str, rel_posix: str, patterns: List[str]) -> bool:
    return any(
        fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel_posix, p) for p in patterns
    )


def norm_key(rel: str) -> str:
    """Clave de comparación: ignora mayúsculas (según SO) y la forma Unicode (NFC/NFD).

    macOS y exFAT/FAT pueden guardar «é» como un carácter o como «e» + acento.
    """
    return unicodedata.normalize("NFC", os.path.normcase(rel))


def counterpart_path(root: str, tree: Dict[str, "FileInfo"], rel: str) -> str:
    """Ruta en `root` para `rel`, reutilizando el nombre real de la carpeta padre si ya existe."""
    parent, _, name = rel.rpartition("/")
    if parent:
        p = tree.get(norm_key(parent.replace("/", os.sep)))
        if p is not None and p.is_dir:
            return os.path.join(p.path, name)
    return join_rel(root, rel)


def mtime_delta(a: "FileInfo", b: "FileInfo", opts: "SyncOptions") -> float:
    """0 si las fechas se consideran iguales; si no, a.mtime - b.mtime.

    Se ignoran diferencias menores que la tolerancia y, si está activado,
    desfases de horas completas (zona horaria / horario de verano en FAT/exFAT).
    """
    diff = a.mtime - b.mtime
    ad = abs(diff)
    if ad <= opts.tolerance:
        return 0.0
    if opts.ignore_tz_shift and ad <= 14 * 3600 + opts.tolerance:
        r = ad % 3600
        if min(r, 3600 - r) <= opts.tolerance:
            return 0.0
    return diff


def make_writable(path: str) -> None:
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    except OSError:
        pass


def file_hash(path: str, check_cancel: Callable[[], None]) -> bytes:
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        while True:
            check_cancel()
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.digest()


# ============================================================================
#  ANÁLISIS: escaneo y construcción del plan
# ============================================================================


def scan_tree(
    root: str,
    opts: SyncOptions,
    check_cancel: Callable[[], None],
    log: Callable[[str], None],
    status: Callable[[str], None],
) -> Dict[str, FileInfo]:
    """Devuelve {clave_normalizada: FileInfo} con todo el contenido de `root`."""
    result: Dict[str, FileInfo] = {}
    stack = [""]
    while stack:
        check_cancel()
        rel_dir = stack.pop()
        abs_dir = os.path.join(root, rel_dir) if rel_dir else root
        try:
            with os.scandir(abs_dir) as it:
                entries = list(it)
        except OSError as e:
            log(f"AVISO: no se puede leer la carpeta «{abs_dir}»: {e}")
            continue
        for e in entries:
            rel = os.path.join(rel_dir, e.name) if rel_dir else e.name
            rel_posix = rel.replace(os.sep, "/")
            if is_ignored(e.name, rel_posix, opts.ignore):
                continue
            try:
                if e.is_symlink():
                    log(f"AVISO: enlace simbólico ignorado: {e.path}")
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
                st = e.stat(follow_symlinks=False)
            except OSError as ex:
                log(f"AVISO: no se puede acceder a «{e.path}»: {ex}")
                continue
            if is_dir and not opts.recursive:
                continue
            if not is_dir and not stat.S_ISREG(st.st_mode):
                log(f"AVISO: elemento especial ignorado: {e.path}")
                continue
            result[norm_key(rel)] = FileInfo(
                rel=rel_posix,
                path=e.path,
                is_dir=is_dir,
                size=0 if is_dir else st.st_size,
                mtime=st.st_mtime,
            )
            if is_dir:
                stack.append(rel)
        status(f"Explorando «{root}»… {len(result)} elementos")
    return result


def compare_files(
    a: FileInfo, b: FileInfo, opts: SyncOptions, check_cancel: Callable[[], None]
) -> List[str]:
    """Lista de motivos por los que `a` y `b` se consideran distintos (vacía = iguales)."""
    reasons: List[str] = []
    if opts.cmp_size and a.size != b.size:
        reasons.append(f"tamaño distinto ({fmt_size(a.size)} / {fmt_size(b.size)})")
    delta = mtime_delta(a, b, opts)
    if opts.cmp_mtime and delta != 0:
        newer = "origen más reciente" if delta > 0 else "destino más reciente"
        reasons.append(f"fecha distinta ({newer})")
    if opts.cmp_hash and not reasons and a.size == b.size:
        if file_hash(a.path, check_cancel) != file_hash(b.path, check_cancel):
            reasons.append("contenido distinto (hash)")
    return reasons


def build_plan(
    opts: SyncOptions,
    check_cancel: Callable[[], None],
    status: Callable[[str], None],
    log: Callable[[str], None],
) -> Plan:
    plan = Plan(options=opts)
    status("Explorando carpeta de origen…")
    A = scan_tree(opts.origin, opts, check_cancel, log, status)
    status("Explorando carpeta de destino…")
    B = scan_tree(opts.dest, opts, check_cancel, log, status)

    def to_dest(rel: str) -> str:
        return counterpart_path(opts.dest, B, rel)

    def to_origin(rel: str) -> str:
        return counterpart_path(opts.origin, A, rel)

    keys = sorted(set(A) | set(B))
    for n, k in enumerate(keys):
        if n % 200 == 0:
            check_cancel()
            status(f"Comparando… {n}/{len(keys)}")
        a, b = A.get(k), B.get(k)

        # ---- Solo existe en el origen -------------------------------------
        if a and not b:
            if a.is_dir:
                plan.items.append(PlanItem(
                    Action.CREATE_DIR, a.rel, "No existe en destino",
                    DIR_ORIGIN_TO_DEST, a, None, a.path, to_dest(a.rel)))
            else:
                plan.items.append(PlanItem(
                    Action.COPY, a.rel, "No existe en destino",
                    DIR_ORIGIN_TO_DEST, a, None, a.path, to_dest(a.rel), a.size))
            continue

        # ---- Solo existe en el destino ------------------------------------
        if b and not a:
            if opts.two_way:
                if b.is_dir:
                    plan.items.append(PlanItem(
                        Action.CREATE_DIR, b.rel, "No existe en origen",
                        DIR_DEST_TO_ORIGIN, None, b, b.path, to_origin(b.rel)))
                else:
                    plan.items.append(PlanItem(
                        Action.COPY, b.rel, "No existe en origen",
                        DIR_DEST_TO_ORIGIN, None, b, b.path, to_origin(b.rel), b.size))
            elif opts.delete_extra:
                act = Action.DELETE_DIR if b.is_dir else Action.DELETE_FILE
                plan.items.append(PlanItem(
                    act, b.rel, "No existe en origen",
                    DIR_DELETE_DEST, None, b, "", b.path))
            else:
                plan.kept_extra += 1
            continue

        # ---- Existe en ambos ----------------------------------------------
        assert a and b
        if a.is_dir != b.is_dir:
            plan.items.append(PlanItem(
                Action.CONFLICT, a.rel,
                "Uno es una carpeta y el otro un archivo: se omite", "", a, b))
            continue
        if a.is_dir:
            continue

        try:
            reasons = compare_files(a, b, opts, check_cancel)
        except OSError as e:
            plan.items.append(PlanItem(
                Action.CONFLICT, a.rel, f"No se pudo leer para comparar: {e}", "", a, b))
            continue
        if not reasons:
            plan.identical += 1
            continue
        reason = "; ".join(reasons)

        if opts.overwrite_policy == POLICY_NEVER:
            plan.items.append(PlanItem(
                Action.SKIP, a.rel,
                reason + " — no se sobrescribe (política «nunca»)", "", a, b))
            continue

        if opts.two_way:
            diff = mtime_delta(a, b, opts)
            if diff != 0:
                if diff > 0:
                    plan.items.append(PlanItem(
                        Action.OVERWRITE, a.rel, reason + "; prevalece el origen (más reciente)",
                        DIR_ORIGIN_TO_DEST, a, b, a.path, b.path, a.size))
                else:
                    plan.items.append(PlanItem(
                        Action.OVERWRITE, a.rel, reason + "; prevalece el destino (más reciente)",
                        DIR_DEST_TO_ORIGIN, a, b, b.path, a.path, b.size))
            else:
                plan.items.append(PlanItem(
                    Action.CONFLICT, a.rel,
                    reason + "; misma fecha, no se puede decidir cuál prevalece",
                    "", a, b))
            continue

        # Unidireccional
        if opts.overwrite_policy == POLICY_NEWER and not (mtime_delta(a, b, opts) > 0):
            plan.items.append(PlanItem(
                Action.SKIP, a.rel,
                reason + " — el destino es igual de reciente o más: no se sobrescribe",
                "", a, b))
            continue
        if mtime_delta(a, b, opts) < 0:
            reason += "  ⚠ el destino es MÁS RECIENTE que el origen"
        plan.items.append(PlanItem(
            Action.OVERWRITE, a.rel, reason,
            DIR_ORIGIN_TO_DEST, a, b, a.path, b.path, a.size))

    plan.items.sort(key=lambda i: (ACTION_ORDER.index(i.action), i.rel.lower()))
    status("Análisis completado")
    return plan


# ============================================================================
#  HILOS DE TRABAJO
# ============================================================================


class AnalyzeWorker(QThread):
    status = Signal(str)
    log = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, opts: SyncOptions):
        super().__init__()
        self.opts = opts
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def _check(self) -> None:
        if self._cancel:
            raise Cancelled()

    def run(self) -> None:
        try:
            plan = build_plan(self.opts, self._check, self.status.emit, self.log.emit)
            self.finished_ok.emit(plan)
        except Cancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class SyncWorker(QThread):
    progress = Signal(int, str)
    log = Signal(str)
    finished_ok = Signal(object)

    CHUNK = 1 << 20
    FIXED_WEIGHT = 64 * 1024  # peso de cada operación para la barra de progreso

    def __init__(self, plan: Plan):
        super().__init__()
        self.items = [i for i in plan.items if i.checked and i.actionable]
        self.use_trash = plan.options.use_trash
        self._cancel = False
        self._done = 0
        self._total = 1
        self._last_emit = 0.0
        self._current = ""

    def cancel(self) -> None:
        self._cancel = True

    # ---- progreso ---------------------------------------------------------
    def _emit_progress(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_emit > 0.05:
            self._last_emit = now
            pct = min(100, int(self._done * 100 / self._total))
            self.progress.emit(pct, self._current)

    def _advance(self, n: int) -> None:
        self._done += n
        self._emit_progress()

    # ---- ejecución --------------------------------------------------------
    def run(self) -> None:
        res = SyncResult()
        creates = sorted((i for i in self.items if i.action == Action.CREATE_DIR),
                         key=lambda i: len(i.to_path))
        copies = [i for i in self.items if i.action in (Action.COPY, Action.OVERWRITE)]
        del_files = [i for i in self.items if i.action == Action.DELETE_FILE]
        del_dirs = sorted((i for i in self.items if i.action == Action.DELETE_DIR),
                          key=lambda i: len(i.to_path), reverse=True)
        sequence = creates + copies + del_files + del_dirs
        self._total = sum(i.size + self.FIXED_WEIGHT for i in sequence) or 1
        self._done = 0

        for it in sequence:
            if self._cancel:
                res.cancelled = True
                break
            start = self._done
            weight = it.size + self.FIXED_WEIGHT
            self._current = f"{it.action.value}: {it.rel}"
            self._emit_progress(force=True)
            try:
                self._execute(it, res)
            except Cancelled:
                res.cancelled = True
                break
            except OSError as e:
                msg = f"{it.action.value} «{it.rel}»: {e}"
                res.errors.append(msg)
                self.log.emit(f"ERROR: {msg}")
            self._done = start + weight

        self._current = "Finalizado"
        self._emit_progress(force=True)
        self.finished_ok.emit(res)

    def _execute(self, it: PlanItem, res: SyncResult) -> None:
        act = it.action
        if act == Action.CREATE_DIR:
            os.makedirs(it.to_path, exist_ok=True)
            self.log.emit(f"[Carpeta creada] {it.rel}")
        elif act in (Action.COPY, Action.OVERWRITE):
            self._copy_file(it.from_path, it.to_path)
            res.bytes_copied += it.size
            tag = "Copiado" if act == Action.COPY else "Sobrescrito"
            self.log.emit(f"[{tag}] {it.rel}  ({it.direction})")
        elif act == Action.DELETE_FILE:
            if not os.path.lexists(it.to_path):
                return
            self._delete(it.to_path, is_dir=False)
            self.log.emit(f"[Eliminado] {it.rel}")
        elif act == Action.DELETE_DIR:
            if not os.path.lexists(it.to_path):
                return
            if os.path.isdir(it.to_path) and os.listdir(it.to_path):
                res.skipped_nonempty += 1
                self.log.emit(
                    f"AVISO: la carpeta «{it.rel}» no está vacía (contiene elementos "
                    "ignorados o excluidos del informe): se conserva")
                return
            self._delete(it.to_path, is_dir=True)
            self.log.emit(f"[Carpeta eliminada] {it.rel}")
        res.done[act] += 1

    def _copy_file(self, src: str, dst: str) -> None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".~sync-tmp"
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    if self._cancel:
                        raise Cancelled()
                    chunk = fi.read(self.CHUNK)
                    if not chunk:
                        break
                    fo.write(chunk)
                    self._advance(len(chunk))
            shutil.copystat(src, tmp)  # conserva fecha de modificación y permisos
            try:
                os.replace(tmp, dst)
            except PermissionError:
                make_writable(dst)
                os.replace(tmp, dst)
        except BaseException:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

    def _delete(self, path: str, is_dir: bool) -> None:
        if self.use_trash:
            r = QFile.moveToTrash(path)
            ok = r[0] if isinstance(r, tuple) else bool(r)
            if not ok:
                raise OSError("no se pudo mover a la papelera")
            return
        if is_dir:
            os.rmdir(path)
        else:
            try:
                os.remove(path)
            except PermissionError:
                make_writable(path)
                os.remove(path)


# ============================================================================
#  WIDGETS
# ============================================================================


class FolderDropEdit(QLineEdit):
    """QLineEdit que acepta carpetas arrastradas desde el explorador de archivos."""

    HIGHLIGHT = "QLineEdit { border: 2px dashed #3d8bfd; }"

    def __init__(self, placeholder: str):
        super().__init__()
        self.setAcceptDrops(True)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.setMinimumHeight(38)

    @staticmethod
    def _dir_from_mime(mime) -> Optional[str]:
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    p = url.toLocalFile()
                    if os.path.isdir(p):
                        return os.path.normpath(p)
        return None

    def dragEnterEvent(self, e) -> None:
        if self._dir_from_mime(e.mimeData()):
            e.acceptProposedAction()
            self.setStyleSheet(self.HIGHLIGHT)
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:
        if self._dir_from_mime(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e) -> None:
        self.setStyleSheet("")
        e.accept()

    def dropEvent(self, e) -> None:
        self.setStyleSheet("")
        p = self._dir_from_mime(e.mimeData())
        if p:
            self.setText(p)
            e.acceptProposedAction()
        else:
            e.ignore()


# ============================================================================
#  ICONO DE LA APLICACIÓN (dibujado por código, sin ficheros externos)
# ============================================================================


def paint_icon(p: QPainter) -> None:
    """Dibuja el icono en un lienzo lógico de 512×512."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)

    # Fondo: cuadrado redondeado con degradado y un ligero brillo superior
    bg = QLinearGradient(0, 24, 0, 488)
    bg.setColorAt(0.0, QColor("#4da3ff"))
    bg.setColorAt(1.0, QColor("#1b57d6"))
    p.setBrush(QBrush(bg))
    p.drawRoundedRect(QRectF(24, 24, 464, 464), 108, 108)
    gloss = QLinearGradient(0, 24, 0, 260)
    gloss.setColorAt(0.0, QColor(255, 255, 255, 70))
    gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
    shape = QPainterPath()
    shape.addRoundedRect(QRectF(24, 24, 464, 464), 108, 108)
    top = QPainterPath()
    top.addRect(QRectF(24, 24, 464, 236))
    p.setBrush(QBrush(gloss))
    p.drawPath(shape.intersected(top))  # el brillo respeta las esquinas redondeadas

    # Dos flechas circulares (símbolo de sincronización)
    cx = cy = 256.0
    r = 150.0
    pen = QPen(QColor("white"), 34)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    arc_rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    th = math.radians(145)
    tip_pt = QPointF(cx + r * math.cos(th), cy - r * math.sin(th))
    d = QPointF(-math.sin(th), -math.cos(th))  # sentido de giro
    n = QPointF(-d.y(), d.x())
    head = QPolygonF([tip_pt + d * 46, tip_pt + n * 44, tip_pt - n * 44])
    for k in range(2):
        p.save()
        p.translate(cx, cy)
        p.rotate(180 * k)
        p.translate(-cx, -cy)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(arc_rect, 25 * 16, 120 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("white"))
        p.drawPolygon(head)
        p.restore()

    # Carpeta en el centro
    back = QPainterPath()
    back.setFillRule(Qt.FillRule.WindingFill)
    back.addRoundedRect(QRectF(181, 208, 68, 40), 11, 11)  # pestaña
    back.addRoundedRect(QRectF(181, 226, 150, 100), 16, 16)
    p.setBrush(QColor("#b9d4ff"))
    p.drawPath(back)
    front = QLinearGradient(0, 248, 0, 326)
    front.setColorAt(0.0, QColor("#ffffff"))
    front.setColorAt(1.0, QColor("#dce9ff"))
    p.setBrush(QBrush(front))
    p.drawRoundedRect(QRectF(181, 248, 150, 78), 16, 16)


def render_icon(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.scale(size / 512, size / 512)
    paint_icon(p)
    p.end()
    return pm


def make_app_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256, 512):
        icon.addPixmap(render_icon(s))
    return icon


def export_icons(folder: str) -> None:
    """Guarda el icono como PNG (varios tamaños) y .ico para empaquetar la aplicación."""
    os.makedirs(folder, exist_ok=True)
    for s in (1024, 512, 256, 128, 64, 32, 16):
        render_icon(s).save(os.path.join(folder, f"folder_sync_{s}.png"))
    render_icon(256).save(os.path.join(folder, "folder_sync.ico"))
    print(f"Iconos guardados en {os.path.abspath(folder)}")


# ============================================================================
#  CUADROS DE MENSAJE
#  En macOS el diálogo nativo (NSAlert) puede cerrar la aplicación con una
#  excepción al dibujar el icono, así que se fuerza el diálogo propio de Qt.
# ============================================================================


class MessageBox(QMessageBox):
    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.setOption(QMessageBox.Option.DontUseNativeDialog, True)


def _simple_box(parent, icon, title, text, buttons=QMessageBox.StandardButton.Ok,
                default=QMessageBox.StandardButton.NoButton):
    box = MessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    if default != QMessageBox.StandardButton.NoButton:
        box.setDefaultButton(default)
    return box.exec()


def msg_warning(parent, title, text):
    return _simple_box(parent, QMessageBox.Icon.Warning, title, text)


def msg_info(parent, title, text):
    return _simple_box(parent, QMessageBox.Icon.Information, title, text)


def msg_critical(parent, title, text):
    return _simple_box(parent, QMessageBox.Icon.Critical, title, text)


def msg_question(parent, title, text):
    return _simple_box(
        parent, QMessageBox.Icon.Question, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No)


# ============================================================================
#  VENTANA PRINCIPAL
# ============================================================================


class MainWindow(QMainWindow):
    COLUMNS = ["Acción", "Ruta relativa", "Dirección", "Tam. origen",
               "Fecha origen", "Tam. destino", "Fecha destino", "Motivo"]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FolderSync – Sincronizador de carpetas")
        self.resize(1280, 860)
        self.plan: Optional[Plan] = None
        self.worker: Optional[QThread] = None
        self._busy = False
        self._building = False
        self.filter_boxes: Dict[Action, QCheckBox] = {}
        self.settings = QSettings("FolderSync", "FolderSync")

        self._build_ui()
        self._load_settings()
        self._connect_invalidation()
        self._update_mode_dependent()
        self._refresh_buttons()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ---- Configuración (se desactiva mientras se trabaja) -------------
        self.config_widget = QWidget()
        cfg = QVBoxLayout(self.config_widget)
        cfg.setContentsMargins(0, 0, 0, 0)

        gb_dirs = QGroupBox("Carpetas (puedes arrastrarlas desde el explorador)")
        grid = QGridLayout(gb_dirs)
        self.edit_origin = FolderDropEdit("Arrastra aquí la carpeta de ORIGEN o pulsa «Examinar…»")
        self.edit_dest = FolderDropEdit("Arrastra aquí la carpeta de DESTINO o pulsa «Examinar…»")
        btn_o = QPushButton("Examinar…")
        btn_d = QPushButton("Examinar…")
        self.btn_swap = QPushButton("⇅  Intercambiar")
        btn_o.clicked.connect(lambda: self._pick_folder(self.edit_origin))
        btn_d.clicked.connect(lambda: self._pick_folder(self.edit_dest))
        self.btn_swap.clicked.connect(self._swap_folders)
        grid.addWidget(QLabel("<b>Origen:</b>"), 0, 0)
        grid.addWidget(self.edit_origin, 0, 1)
        grid.addWidget(btn_o, 0, 2)
        grid.addWidget(self.btn_swap, 1, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(QLabel("<b>Destino:</b>"), 2, 0)
        grid.addWidget(self.edit_dest, 2, 1)
        grid.addWidget(btn_d, 2, 2)
        grid.setColumnStretch(1, 1)
        cfg.addWidget(gb_dirs)

        opts_row = QHBoxLayout()

        gb_cmp = QGroupBox("Modo y criterios de comparación")
        lay = QVBoxLayout(gb_cmp)
        self.rb_oneway = QRadioButton("Unidireccional: origen → destino")
        self.rb_twoway = QRadioButton("Bidireccional: ambos sentidos (prevalece el más reciente)")
        self.rb_oneway.setChecked(True)
        lay.addWidget(self.rb_oneway)
        lay.addWidget(self.rb_twoway)
        lay.addSpacing(6)
        lay.addWidget(QLabel("Un archivo se considera distinto si difiere en alguno de:"))
        row = QHBoxLayout()
        self.chk_mtime = QCheckBox("Fecha de modificación")
        self.chk_mtime.setChecked(True)
        self.spin_tol = QDoubleSpinBox()
        self.spin_tol.setRange(0, 3600)
        self.spin_tol.setDecimals(1)
        self.spin_tol.setValue(2.0)
        self.spin_tol.setSuffix(" s")
        self.spin_tol.setToolTip(
            "Diferencias de fecha menores que este valor se ignoran.\n"
            "2 s es lo adecuado para sistemas de ficheros FAT/exFAT.")
        row.addWidget(self.chk_mtime)
        row.addWidget(QLabel("tolerancia:"))
        row.addWidget(self.spin_tol)
        row.addStretch(1)
        lay.addLayout(row)
        self.chk_tz = QCheckBox("Ignorar desfases de horas completas (típico de FAT/exFAT)")
        self.chk_tz.setChecked(True)
        self.chk_tz.setToolTip(
            "exFAT/FAT guardan la fecha en hora local, y al leerla en otro sistema o tras un\n"
            "cambio de horario puede aparecer desplazada exactamente 1, 2… horas.\n"
            "Con esto, esas diferencias no cuentan como «fecha distinta».")
        lay.addWidget(self.chk_tz)
        self.chk_size = QCheckBox("Tamaño")
        self.chk_size.setChecked(True)
        self.chk_hash = QCheckBox("Contenido (hash) – más lento, lee todos los archivos")
        lay.addWidget(self.chk_size)
        lay.addWidget(self.chk_hash)
        lay.addStretch(1)
        opts_row.addWidget(gb_cmp, 1)

        gb_act = QGroupBox("Acciones y opciones")
        form = QFormLayout(gb_act)
        self.combo_policy = QComboBox()
        self.combo_policy.addItem("Sobrescribir cuando los archivos difieran", POLICY_DIFFERENT)
        self.combo_policy.addItem("Sobrescribir solo si el origen es más reciente", POLICY_NEWER)
        self.combo_policy.addItem("No sobrescribir nunca (solo copiar lo que falta)", POLICY_NEVER)
        self.combo_policy.setToolTip(
            "En modo bidireccional las dos primeras opciones equivalen a «prevalece el más "
            "reciente»; los archivos distintos con la misma fecha se marcan como conflicto.")
        form.addRow("Si existe en ambos:", self.combo_policy)
        self.chk_delete = QCheckBox("Eliminar en destino lo que no exista en origen (espejo)")
        self.chk_trash = QCheckBox("Enviar a la papelera en lugar de borrar definitivamente")
        self.chk_trash.setChecked(True)
        self.chk_recursive = QCheckBox("Incluir subcarpetas")
        self.chk_recursive.setChecked(True)
        form.addRow(self.chk_delete)
        form.addRow(self.chk_trash)
        form.addRow(self.chk_recursive)
        self.edit_ignore = QLineEdit("Thumbs.db, .DS_Store, desktop.ini, ._*")
        self.edit_ignore.setToolTip(
            "Patrones tipo glob separados por comas (p. ej. *.tmp, node_modules, .git).\n"
            "Se aplican al nombre y a la ruta relativa de archivos y carpetas.")
        form.addRow("Ignorar:", self.edit_ignore)
        opts_row.addWidget(gb_act, 1)
        cfg.addLayout(opts_row)
        root.addWidget(self.config_widget)

        # ---- Botones ------------------------------------------------------
        btns = QHBoxLayout()
        self.btn_analyze = QPushButton("🔍  Analizar (F5)")
        self.btn_analyze.setShortcut("F5")
        self.btn_sync = QPushButton("▶  Sincronizar")
        self.btn_cancel = QPushButton("■  Cancelar")
        self.btn_export = QPushButton("Exportar informe…")
        for b in (self.btn_analyze, self.btn_sync, self.btn_cancel):
            b.setMinimumHeight(36)
        self.btn_analyze.clicked.connect(self._start_analysis)
        self.btn_sync.clicked.connect(self._start_sync)
        self.btn_cancel.clicked.connect(self._cancel_work)
        self.btn_export.clicked.connect(self._export_report)
        btns.addWidget(self.btn_analyze)
        btns.addWidget(self.btn_sync)
        btns.addWidget(self.btn_cancel)
        btns.addStretch(1)
        btns.addWidget(self.btn_export)
        root.addLayout(btns)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.lbl_status = QLabel("Selecciona las carpetas y pulsa «Analizar».")
        root.addWidget(self.progress)
        root.addWidget(self.lbl_status)

        # ---- Pestañas: informe / registro ---------------------------------
        self.tabs = QTabWidget()
        report = QWidget()
        rl = QVBoxLayout(report)
        self.lbl_summary = QLabel("")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setTextFormat(Qt.TextFormat.RichText)
        rl.addWidget(self.lbl_summary)

        frow = QHBoxLayout()
        frow.addWidget(QLabel("Mostrar:"))
        for act in ACTION_ORDER:
            cb = QCheckBox(act.value)
            cb.setChecked(True)
            cb.setVisible(False)
            cb.toggled.connect(self._apply_filter)
            self.filter_boxes[act] = cb
            frow.addWidget(cb)
        frow.addStretch(1)
        b_all = QPushButton("Marcar visibles")
        b_none = QPushButton("Desmarcar visibles")
        b_all.clicked.connect(lambda: self._set_visible_checked(True))
        b_none.clicked.connect(lambda: self._set_visible_checked(False))
        frow.addWidget(b_all)
        frow.addWidget(b_none)
        rl.addLayout(frow)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLUMNS))
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, w in enumerate([160, 380, 130, 90, 140, 90, 140, 400]):
            self.tree.setColumnWidth(i, w)
        self.tree.itemChanged.connect(self._on_item_changed)
        rl.addWidget(self.tree, 1)
        self.tabs.addTab(report, "Informe previo")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(50000)
        self.tabs.addTab(self.log_view, "Registro")
        root.addWidget(self.tabs, 1)

    # --------------------------------------------------- ajustes persistentes
    def _load_settings(self) -> None:
        s = self.settings
        self.edit_origin.setText(s.value("origin", "", type=str))
        self.edit_dest.setText(s.value("dest", "", type=str))
        (self.rb_twoway if s.value("two_way", False, type=bool) else self.rb_oneway).setChecked(True)
        self.chk_mtime.setChecked(s.value("mtime", True, type=bool))
        self.chk_size.setChecked(s.value("size", True, type=bool))
        self.chk_hash.setChecked(s.value("hash", False, type=bool))
        self.spin_tol.setValue(s.value("tol", 2.0, type=float))
        self.chk_tz.setChecked(s.value("tz", True, type=bool))
        self.combo_policy.setCurrentIndex(s.value("policy", 0, type=int))
        self.chk_delete.setChecked(s.value("delete", False, type=bool))
        self.chk_trash.setChecked(s.value("trash", True, type=bool))
        self.chk_recursive.setChecked(s.value("recursive", True, type=bool))
        self.edit_ignore.setText(s.value("ignore", self.edit_ignore.text(), type=str))
        geo = s.value("geometry")
        if geo:
            self.restoreGeometry(geo)

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("origin", self.edit_origin.text())
        s.setValue("dest", self.edit_dest.text())
        s.setValue("two_way", self.rb_twoway.isChecked())
        s.setValue("mtime", self.chk_mtime.isChecked())
        s.setValue("size", self.chk_size.isChecked())
        s.setValue("hash", self.chk_hash.isChecked())
        s.setValue("tol", self.spin_tol.value())
        s.setValue("tz", self.chk_tz.isChecked())
        s.setValue("policy", self.combo_policy.currentIndex())
        s.setValue("delete", self.chk_delete.isChecked())
        s.setValue("trash", self.chk_trash.isChecked())
        s.setValue("recursive", self.chk_recursive.isChecked())
        s.setValue("ignore", self.edit_ignore.text())
        s.setValue("geometry", self.saveGeometry())

    # ------------------------------------------------------------ invalidar
    def _connect_invalidation(self) -> None:
        for w in (self.edit_origin, self.edit_dest, self.edit_ignore):
            w.textChanged.connect(self._invalidate)
        for w in (self.rb_oneway, self.rb_twoway, self.chk_mtime, self.chk_size,
                  self.chk_hash, self.chk_delete, self.chk_trash, self.chk_recursive,
                  self.chk_tz):
            w.toggled.connect(self._invalidate)
        self.spin_tol.valueChanged.connect(self._invalidate)
        self.combo_policy.currentIndexChanged.connect(self._invalidate)
        self.rb_twoway.toggled.connect(self._update_mode_dependent)
        self.chk_mtime.toggled.connect(lambda c: (self.spin_tol.setEnabled(c), self.chk_tz.setEnabled(c)))

    def _invalidate(self, *_args) -> None:
        if self.plan is not None and not self._busy:
            self.plan = None
            self.lbl_status.setText(
                "La configuración ha cambiado: vuelve a pulsar «Analizar» para generar un nuevo informe.")
            self._refresh_buttons()

    def _update_mode_dependent(self, *_args) -> None:
        two = self.rb_twoway.isChecked()
        self.chk_delete.setEnabled(not two)
        self.chk_delete.setToolTip(
            "No disponible en modo bidireccional: sin un historial de sincronizaciones "
            "no se puede saber si un archivo se borró o es nuevo." if two else "")

    # --------------------------------------------------------- carpetas
    def _pick_folder(self, edit: QLineEdit) -> None:
        start = edit.text() or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "Selecciona una carpeta", start)
        if d:
            edit.setText(os.path.normpath(d))

    def _swap_folders(self) -> None:
        o, d = self.edit_origin.text(), self.edit_dest.text()
        self.edit_origin.setText(d)
        self.edit_dest.setText(o)

    def _collect_options(self) -> Optional[SyncOptions]:
        origin = self.edit_origin.text().strip().strip('"')
        dest = self.edit_dest.text().strip().strip('"')
        if not origin or not dest:
            msg_warning(self, "Faltan carpetas",
                                "Indica la carpeta de origen y la de destino.")
            return None
        for label, p in (("origen", origin), ("destino", dest)):
            if not os.path.isdir(p):
                msg_warning(self, "Carpeta no válida",
                                    f"La carpeta de {label} no existe:\n{p}")
                return None
        if is_nested_or_same(origin, dest):
            msg_warning(
                self, "Carpetas no válidas",
                "El origen y el destino son la misma carpeta o una está dentro de la otra.\n"
                "Eso provocaría copias recursivas, así que no está permitido.")
            return None
        if not (self.chk_mtime.isChecked() or self.chk_size.isChecked()
                or self.chk_hash.isChecked()):
            msg_warning(self, "Sin criterios",
                                "Selecciona al menos un criterio de comparación.")
            return None
        patterns = [p.strip() for p in self.edit_ignore.text().split(",") if p.strip()]
        return SyncOptions(
            origin=os.path.abspath(origin),
            dest=os.path.abspath(dest),
            two_way=self.rb_twoway.isChecked(),
            recursive=self.chk_recursive.isChecked(),
            cmp_mtime=self.chk_mtime.isChecked(),
            cmp_size=self.chk_size.isChecked(),
            cmp_hash=self.chk_hash.isChecked(),
            tolerance=self.spin_tol.value(),
            ignore_tz_shift=self.chk_tz.isChecked(),
            overwrite_policy=self.combo_policy.currentData(),
            delete_extra=self.chk_delete.isChecked() and not self.rb_twoway.isChecked(),
            use_trash=self.chk_trash.isChecked(),
            ignore=patterns,
        )

    # ----------------------------------------------------------- estados
    def _refresh_buttons(self) -> None:
        busy = self._busy
        has_plan = self.plan is not None
        actionable = has_plan and any(i.checked and i.actionable for i in self.plan.items)
        self.config_widget.setEnabled(not busy)
        self.btn_analyze.setEnabled(not busy)
        self.btn_sync.setEnabled(not busy and actionable)
        self.btn_cancel.setEnabled(busy)
        self.btn_export.setEnabled(not busy and has_plan)
        self.tabs.widget(0).setEnabled(not busy)

    def _log(self, msg: str) -> None:
        self.log_view.appendPlainText(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    def _cancel_work(self) -> None:
        if self.worker is not None and self._busy:
            self.worker.cancel()  # type: ignore[attr-defined]
            self.lbl_status.setText("Cancelando…")
            self.btn_cancel.setEnabled(False)

    # ----------------------------------------------------------- análisis
    def _start_analysis(self) -> None:
        opts = self._collect_options()
        if opts is None:
            return
        if self.worker is not None:
            self.worker.wait()
        self.plan = None
        self.tree.clear()
        self.lbl_summary.setText("")
        for cb in self.filter_boxes.values():
            cb.setVisible(False)
        self.log_view.clear()
        self._log("Iniciando análisis…")
        self.progress.setRange(0, 0)  # indeterminado
        self._busy = True
        self._refresh_buttons()

        w = AnalyzeWorker(opts)
        w.status.connect(self.lbl_status.setText)
        w.log.connect(self._log)
        w.finished_ok.connect(self._on_plan_ready)
        w.failed.connect(self._on_analysis_failed)
        w.cancelled.connect(self._on_analysis_cancelled)
        self.worker = w
        w.start()

    def _on_analysis_failed(self, msg: str) -> None:
        self._busy = False
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_status.setText("Error durante el análisis.")
        self._log(f"ERROR: {msg}")
        self._refresh_buttons()
        msg_critical(self, "Error", f"El análisis ha fallado:\n{msg}")

    def _on_analysis_cancelled(self) -> None:
        self._busy = False
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_status.setText("Análisis cancelado.")
        self._log("Análisis cancelado por el usuario.")
        self._refresh_buttons()

    def _on_plan_ready(self, plan: Plan) -> None:
        self.plan = plan
        self._busy = False
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._populate_tree()
        self._update_summary()
        self.tabs.setCurrentIndex(0)
        n_act = sum(1 for i in plan.items if i.actionable)
        if n_act == 0:
            self.lbl_status.setText("No hay nada que sincronizar: las carpetas ya están al día.")
        else:
            self.lbl_status.setText(
                "Análisis completado. Revisa el informe y pulsa «Sincronizar» si estás de acuerdo.")
        self._log(f"Análisis completado: {len(plan.items)} operaciones/avisos, "
                  f"{plan.identical} archivos idénticos.")
        self._refresh_buttons()

    # ------------------------------------------------------------- informe
    def _populate_tree(self) -> None:
        assert self.plan is not None
        self._building = True
        self.tree.clear()
        counts = Counter(i.action for i in self.plan.items)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

        def sz(i: Optional[FileInfo]) -> str:
            return "" if (i is None or i.is_dir) else fmt_size(i.size)

        def dt(i: Optional[FileInfo]) -> str:
            return "" if i is None else fmt_time(i.mtime)

        rows: List[QTreeWidgetItem] = []
        for idx, it in enumerate(self.plan.items):
            name = it.rel + ("/" if it.is_dir else "")
            ti = QTreeWidgetItem([
                f"{ACTION_ICON[it.action]}  {it.action.value}", name, it.direction,
                sz(it.a_info), dt(it.a_info), sz(it.b_info), dt(it.b_info), it.reason,
            ])
            ti.setData(0, Qt.ItemDataRole.UserRole, idx)
            if it.actionable:
                ti.setFlags(ti.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                ti.setCheckState(0, Qt.CheckState.Checked if it.checked
                                 else Qt.CheckState.Unchecked)
            brush = QBrush(ACTION_COLOR[it.action])
            for c in range(len(self.COLUMNS)):
                ti.setBackground(c, brush)
            ti.setTextAlignment(3, right)
            ti.setTextAlignment(5, right)
            tip = []
            if it.from_path:
                tip.append(f"Desde: {it.from_path}")
            if it.to_path:
                tip.append(f"Hacia: {it.to_path}")
            tip.append(it.reason)
            ti.setToolTip(1, "\n".join(t for t in tip if t))
            ti.setToolTip(7, it.reason)
            rows.append(ti)
        self.tree.addTopLevelItems(rows)

        for act, cb in self.filter_boxes.items():
            n = counts.get(act, 0)
            cb.blockSignals(True)
            cb.setText(f"{act.value} ({n})")
            cb.setChecked(True)
            cb.setVisible(n > 0)
            cb.blockSignals(False)
        self._building = False

    def _apply_filter(self) -> None:
        if self.plan is None:
            return
        for i in range(self.tree.topLevelItemCount()):
            ti = self.tree.topLevelItem(i)
            idx = ti.data(0, Qt.ItemDataRole.UserRole)
            ti.setHidden(not self.filter_boxes[self.plan.items[idx].action].isChecked())

    def _set_visible_checked(self, state: bool) -> None:
        if self.plan is None:
            return
        self._building = True
        for i in range(self.tree.topLevelItemCount()):
            ti = self.tree.topLevelItem(i)
            if ti.isHidden():
                continue
            idx = ti.data(0, Qt.ItemDataRole.UserRole)
            it = self.plan.items[idx]
            if it.actionable:
                it.checked = state
                ti.setCheckState(0, Qt.CheckState.Checked if state else Qt.CheckState.Unchecked)
        self._building = False
        self._update_summary()
        self._refresh_buttons()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._building or column != 0 or self.plan is None:
            return
        idx = item.data(0, Qt.ItemDataRole.UserRole)
        self.plan.items[idx].checked = item.checkState(0) == Qt.CheckState.Checked
        self._update_summary()
        self._refresh_buttons()

    def _stats(self) -> dict:
        p = self.plan
        assert p is not None
        c: Counter = Counter()
        nbytes = 0
        unchecked = 0
        for it in p.items:
            if not it.actionable:
                continue
            if it.checked:
                c[it.action] += 1
                if it.action in (Action.COPY, Action.OVERWRITE):
                    nbytes += it.size
            else:
                unchecked += 1
        return {
            "c": c, "bytes": nbytes, "unchecked": unchecked,
            "conflicts": sum(1 for i in p.items if i.action == Action.CONFLICT),
            "skipped": sum(1 for i in p.items if i.action == Action.SKIP),
            "identical": p.identical, "kept": p.kept_extra,
        }

    def _summary_html(self) -> str:
        s = self._stats()
        c = s["c"]
        lines = [
            "<b>Se va a:</b> "
            f"crear <b>{c[Action.CREATE_DIR]}</b> carpetas · "
            f"copiar <b>{c[Action.COPY]}</b> archivos nuevos · "
            f"sobrescribir <b>{c[Action.OVERWRITE]}</b> archivos · "
            f"<span style='color:#d9534f'>eliminar <b>{c[Action.DELETE_FILE]}</b> archivos y "
            f"<b>{c[Action.DELETE_DIR]}</b> carpetas</span>",
            f"Datos a transferir: <b>{fmt_size(s['bytes'])}</b>",
        ]
        extra = [f"{s['identical']} archivos idénticos (sin cambios)"]
        if s["kept"]:
            extra.append(f"{s['kept']} elementos solo en destino que se conservan")
        if s["skipped"]:
            extra.append(f"{s['skipped']} archivos distintos omitidos por la política elegida")
        if s["conflicts"]:
            extra.append(f"<span style='color:#b05fd0'><b>{s['conflicts']} conflictos</b> "
                         "(no se tocan)</span>")
        if s["unchecked"]:
            extra.append(f"{s['unchecked']} operaciones desmarcadas manualmente")
        lines.append(" · ".join(extra))
        return "<br>".join(lines)

    def _update_summary(self) -> None:
        if self.plan is not None:
            self.lbl_summary.setText(self._summary_html())

    # ------------------------------------------------------- sincronizar
    def _start_sync(self) -> None:
        if self.plan is None:
            return
        selected = [i for i in self.plan.items if i.checked and i.actionable]
        if not selected:
            msg_info(self, "Nada que hacer",
                                    "No hay operaciones marcadas en el informe.")
            return
        s = self._stats()
        n_del = s["c"][Action.DELETE_FILE] + s["c"][Action.DELETE_DIR]
        box = MessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning if n_del else QMessageBox.Icon.Question)
        box.setWindowTitle("Confirmar sincronización")
        box.setTextFormat(Qt.TextFormat.RichText)
        o = self.plan.options
        mode = "bidireccional" if o.two_way else "origen → destino"
        text = (f"<b>¿Ejecutar la sincronización ({mode})?</b><br><br>"
                f"Origen: {o.origin}<br>Destino: {o.dest}<br><br>"
                f"{self._summary_html()}")
        if n_del:
            how = ("se enviarán a la papelera" if o.use_trash
                   else "se borrarán DEFINITIVAMENTE")
            text += f"<br><br><b>Atención:</b> {n_del} elementos {how}."
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        if self.worker is not None:
            self.worker.wait()
        self._log(f"Iniciando sincronización de {len(selected)} operaciones…")
        self.tabs.setCurrentIndex(1)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._busy = True
        self._refresh_buttons()
        w = SyncWorker(self.plan)
        w.progress.connect(self._on_sync_progress)
        w.log.connect(self._log)
        w.finished_ok.connect(self._on_sync_finished)
        self.worker = w
        w.start()

    def _on_sync_progress(self, pct: int, text: str) -> None:
        self.progress.setValue(pct)
        self.lbl_status.setText(text)

    def _on_sync_finished(self, res: SyncResult) -> None:
        self._busy = False
        self.plan = None  # el informe ya no es válido
        d = res.done
        summary = (
            f"Carpetas creadas: {d[Action.CREATE_DIR]}\n"
            f"Archivos copiados: {d[Action.COPY]}\n"
            f"Archivos sobrescritos: {d[Action.OVERWRITE]}\n"
            f"Archivos eliminados: {d[Action.DELETE_FILE]}\n"
            f"Carpetas eliminadas: {d[Action.DELETE_DIR]}\n"
            f"Datos transferidos: {fmt_size(res.bytes_copied)}\n"
            f"Errores: {len(res.errors)}"
        )
        if res.skipped_nonempty:
            summary += f"\nCarpetas conservadas por no estar vacías: {res.skipped_nonempty}"
        self._log("---- Resultado ----\n" + summary)
        if res.cancelled:
            self.lbl_status.setText("Sincronización cancelada por el usuario.")
            title, icon = "Sincronización cancelada", QMessageBox.Icon.Warning
        elif res.errors:
            self.lbl_status.setText(f"Sincronización terminada con {len(res.errors)} errores.")
            title, icon = "Terminada con errores", QMessageBox.Icon.Warning
        else:
            self.progress.setValue(100)
            self.lbl_status.setText("Sincronización completada. Pulsa «Analizar» para verificar.")
            title, icon = "Sincronización completada", QMessageBox.Icon.Information
        self._refresh_buttons()
        box = MessageBox(icon, title, summary, QMessageBox.StandardButton.Ok, self)
        if res.errors:
            box.setDetailedText("\n".join(res.errors))
        box.exec()

    # ------------------------------------------------------------ exportar
    def _report_text(self) -> str:
        p = self.plan
        assert p is not None
        o = p.options
        s = self._stats()
        c = s["c"]
        crit = [n for n, v in (("fecha", o.cmp_mtime), ("tamaño", o.cmp_size),
                               ("contenido", o.cmp_hash)) if v]
        pol = {POLICY_DIFFERENT: "sobrescribir si difieren",
               POLICY_NEWER: "sobrescribir solo si el origen es más reciente",
               POLICY_NEVER: "no sobrescribir nunca"}[o.overwrite_policy]
        out = [
            "INFORME DE SINCRONIZACIÓN",
            f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
            f"Origen:  {o.origin}",
            f"Destino: {o.dest}",
            f"Modo: {'bidireccional' if o.two_way else 'unidireccional (origen → destino)'}",
            f"Criterios: {', '.join(crit)}"
            + (f" (tolerancia {o.tolerance:g} s"
               f"{', ignorando desfases de horas completas' if o.ignore_tz_shift else ''})"
               if o.cmp_mtime else ""),
            f"Política de sobrescritura: {pol}",
            f"Eliminar sobrantes en destino: {'sí' if o.delete_extra else 'no'}"
            f" ({'papelera' if o.use_trash else 'definitivo'})",
            f"Ignorar: {', '.join(o.ignore) or '(nada)'}",
            "",
            "RESUMEN (operaciones marcadas)",
            f"  Carpetas a crear:       {c[Action.CREATE_DIR]}",
            f"  Archivos a copiar:      {c[Action.COPY]}",
            f"  Archivos a sobrescribir: {c[Action.OVERWRITE]}",
            f"  Archivos a eliminar:    {c[Action.DELETE_FILE]}",
            f"  Carpetas a eliminar:    {c[Action.DELETE_DIR]}",
            f"  Datos a transferir:     {fmt_size(s['bytes'])}",
            f"  Idénticos: {s['identical']} · Omitidos: {s['skipped']} · "
            f"Conflictos: {s['conflicts']} · Solo en destino conservados: {s['kept']}",
            "",
            "DETALLE",
        ]
        for it in p.items:
            mark = "[x]" if (it.actionable and it.checked) else (
                "[ ]" if it.actionable else "   ")
            size = f" ({fmt_size(it.size)})" if it.action in (Action.COPY, Action.OVERWRITE) else ""
            out.append(f"{mark} {it.action.value:<17} {it.rel}{'/' if it.is_dir else ''}{size}")
            if it.direction:
                out.append(f"      {it.direction}")
            if it.reason:
                out.append(f"      Motivo: {it.reason}")
        return "\n".join(out) + "\n"

    def _export_report(self) -> None:
        if self.plan is None:
            return
        name = f"informe_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportar informe", os.path.join(os.path.expanduser("~"), name),
            "Texto (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._report_text())
            self.lbl_status.setText(f"Informe exportado a {path}")
        except OSError as e:
            msg_critical(self, "Error", f"No se pudo guardar el informe:\n{e}")

    # --------------------------------------------------------------- cierre
    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker is not None and self._busy:
            if msg_question(
                self, "Operación en curso",
                "Hay una operación en curso. ¿Cancelar y salir?"
            ) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()  # type: ignore[attr-defined]
        if self.worker is not None:
            self.worker.wait(5000)
        self._save_settings()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    if "--export-icon" in sys.argv:
        i = sys.argv.index("--export-icon")
        export_icons(sys.argv[i + 1] if i + 1 < len(sys.argv) else "icons")
        return
    app.setApplicationName("FolderSync")
    app.setWindowIcon(make_app_icon())
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
