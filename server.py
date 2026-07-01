"""Servidor MCP para controlar Paint de Windows 11.

Expone tools para abrir Paint y dibujar en el lienzo mediante automatizacion de GUI
(pyautogui mueve el mouse en coordenadas de pantalla). Las coordenadas que reciben las
tools son RELATIVAS al lienzo: (0, 0) es la esquina superior izquierda del area dibujable.

AUTO-CALIBRACION: el servidor detecta solo la region del lienzo (busca el area blanca
dominante dentro de la ventana) y ubica el icono del Lapiz por reconocimiento de imagen
(cv2.matchTemplate sobre assets/pencil.png). Asi funciona en distinta resolucion/posicion
sin recalibrar. Si la deteccion falla, cae a margenes/coordenadas fijas configurables por
variables de entorno (PAINT_MARGIN_*, PAINT_PENCIL_*).
"""

import ctypes
import os
import subprocess
import time

# IMPORTANTE: declarar el proceso DPI-aware ANTES de importar pyautogui. Si no, en pantallas
# con escala != 100% (p.ej. 125%) Windows "virtualiza" la resolucion: el screenshot sale
# escalado mientras las coordenadas de ventana son fisicas, y la auto-deteccion (lienzo +
# plantilla del lapiz) falla. Hacerlo aqui hace que el screenshot y las coordenadas coincidan.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # SYSTEM DPI aware (Win 8.1+)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # system DPI aware (fallback Win Vista+)
    except Exception:
        pass

import pyautogui
import pygetwindow as gw
import win32api
import win32con
import win32process
from mcp.server.fastmcp import FastMCP

# Dependencias opcionales para auto-calibracion (deteccion por imagen). Si faltan, se usan
# los fallbacks de margenes/coordenadas fijas.
try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover
    cv2 = None
    np = None

# --- Seguridad / comportamiento de pyautogui ---
pyautogui.FAILSAFE = True   # mover el mouse a una esquina aborta (rescate de emergencia)
pyautogui.PAUSE = 0.05      # pausa entre llamadas de pyautogui

# --- Fallback de calibracion (px). Solo se usan si la auto-deteccion falla. ---
# Descuentan el borde de la ventana, el panel de herramientas izquierdo, el ribbon
# superior y la barra de estado inferior. Valores medidos en 2560x1080 al 100%.
MARGIN_TOP = int(os.environ.get("PAINT_MARGIN_TOP", "218"))
MARGIN_LEFT = int(os.environ.get("PAINT_MARGIN_LEFT", "277"))
MARGIN_RIGHT = int(os.environ.get("PAINT_MARGIN_RIGHT", "285"))
MARGIN_BOTTOM = int(os.environ.get("PAINT_MARGIN_BOTTOM", "135"))

# Fallback de la posicion del icono "Lapiz", relativa a la esquina de la ventana.
PENCIL_X = int(os.environ.get("PAINT_PENCIL_X", "262"))
PENCIL_Y = int(os.environ.get("PAINT_PENCIL_Y", "87"))

# Duracion (s) de cada segmento de trazo. Si es muy bajo, Paint puede no registrar el dibujo.
DRAW_DURATION = float(os.environ.get("PAINT_DRAW_DURATION", "0.4"))

# Plantilla del icono del Lapiz para reconocimiento por imagen.
PENCIL_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "pencil.png")

# Cache de la region del lienzo detectada, keyed por geometria de ventana (evita re-screenshot
# por cada punto de un trazo).
_canvas_cache = {}

mcp = FastMCP("paint")


# ----------------------------- Helpers internos -----------------------------

def _window_exe(win):
    """Nombre del ejecutable (basename, lower) dueno de la ventana, o '' si falla."""
    try:
        _, pid = win32process.GetWindowThreadProcessId(win._hWnd)
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        path = win32process.GetModuleFileNameEx(handle, 0)
        return os.path.basename(str(path)).lower()
    except Exception:
        return ""


def _get_paint_window():
    """Devuelve la ventana de Paint o None.

    Hace match por el PROCESO (mspaint.exe), no por el titulo: el titulo de otras ventanas
    (p.ej. VS Code abierto en una carpeta 'MCP-TEST-Paint') tambien contiene 'paint' y
    provocaba que se controlara la ventana equivocada.
    """
    for w in gw.getAllWindows():
        if not w.visible:
            continue
        if _window_exe(w) == "mspaint.exe":
            return w
    return None


def _require_paint():
    """Obtiene la ventana de Paint o lanza error claro si no esta abierta."""
    win = _get_paint_window()
    if win is None:
        raise RuntimeError(
            "No encontre la ventana de Paint. Llama primero a open_paint()."
        )
    return win


def _focus_paint():
    """Enfoca y maximiza la ventana de Paint, devuelve la ventana."""
    win = _require_paint()
    try:
        if win.isMinimized:
            win.restore()
        win.maximize()
        win.activate()
    except Exception:
        # pygetwindow puede lanzar en algunos casos; seguimos igual.
        pass
    time.sleep(0.4)
    return win


def _find_template(path, threshold=0.8):
    """Ubica una plantilla en pantalla con cv2.matchTemplate. Devuelve (cx, cy) o None.

    Devuelve el centro de la MEJOR coincidencia si su score supera `threshold`.
    """
    if cv2 is None or np is None or not os.path.exists(path):
        return None
    try:
        shot = pyautogui.screenshot()
        hay = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
        needle = cv2.imread(path)
        if needle is None:
            return None
        res = cv2.matchTemplate(hay, needle, cv2.TM_CCOEFF_NORMED)
        _, maxval, _, maxloc = cv2.minMaxLoc(res)
        if maxval < threshold:
            return None
        h, w = needle.shape[:2]
        return (maxloc[0] + w // 2, maxloc[1] + h // 2)
    except Exception:
        return None


def _longest_true_run(mask):
    """(start, end) del tramo contiguo mas largo de True en una lista/array booleano."""
    best = (0, 0)
    cur = None
    for i, v in enumerate(mask):
        if v:
            if cur is None:
                cur = i
        elif cur is not None:
            if i - cur > best[1] - best[0]:
                best = (cur, i)
            cur = None
    n = len(mask)
    if cur is not None and n - cur > best[1] - best[0]:
        best = (cur, n)
    return best


def _detect_canvas_region(win):
    """Detecta el lienzo (banda blanca dominante dentro de la ventana). (l,t,w,h) o None.

    El lienzo es la mayor zona blanca pura (255,255,255); el fondo de la app alrededor es
    gris claro. Se aisla la banda de filas y columnas con mas pixeles blancos.
    """
    if np is None:
        return None
    try:
        shot = pyautogui.screenshot()
        arr = np.asarray(shot)[:, :, :3]
        H, W = arr.shape[:2]
        x0, x1 = max(0, win.left), min(W, win.left + win.width)
        y0, y1 = max(0, win.top), min(H, win.top + win.height)
        if x1 - x0 < 50 or y1 - y0 < 50:
            return None
        sub = arr[y0:y1, x0:x1]
        white = (sub[:, :, 0] >= 250) & (sub[:, :, 1] >= 250) & (sub[:, :, 2] >= 250)
        row_white = white.sum(axis=1)
        if row_white.max() < (x1 - x0) * 0.5:
            return None  # no hay una banda blanca lo bastante ancha -> no detectado
        row_mask = row_white > row_white.max() * 0.5
        rt, rb = _longest_true_run(list(row_mask))
        band = white[rt:rb, :]
        col_white = band.sum(axis=0)
        col_mask = col_white > band.shape[0] * 0.5
        cl, cr = _longest_true_run(list(col_mask))
        left, top = x0 + cl, y0 + rt
        width, height = cr - cl, rb - rt
        if width < 50 or height < 50:
            return None
        return (left, top, width, height)
    except Exception:
        return None


def _canvas_region(win):
    """(left, top, width, height) del lienzo en pantalla. Auto-detecta y cachea por geometria.

    Si la deteccion por imagen falla, cae a los margenes fijos (MARGIN_*).
    """
    key = (win.left, win.top, win.width, win.height)
    if key not in _canvas_cache:
        detected = _detect_canvas_region(win)
        if detected is None:
            detected = (
                win.left + MARGIN_LEFT,
                win.top + MARGIN_TOP,
                max(1, win.width - MARGIN_LEFT - MARGIN_RIGHT),
                max(1, win.height - MARGIN_TOP - MARGIN_BOTTOM),
            )
        _canvas_cache[key] = detected
    return _canvas_cache[key]


def _to_screen(win, x, y):
    """Convierte coordenadas relativas al lienzo a coordenadas de pantalla (con clamp)."""
    left, top, width, height = _canvas_region(win)
    sx = left + max(0, min(int(x), width))
    sy = top + max(0, min(int(y), height))
    return sx, sy


def _select_pencil(win):
    """Selecciona la herramienta Lapiz. Ubica su icono por imagen; si no, usa coordenadas.

    Tambien deselecciona cualquier seleccion activa (p.ej. tras un Ctrl+A en clear_canvas),
    que de otro modo haria que los arrastres muevan la seleccion en vez de dibujar.
    """
    loc = _find_template(PENCIL_TEMPLATE)
    if loc is not None:
        pyautogui.click(loc[0], loc[1])
    else:
        pyautogui.click(win.left + PENCIL_X, win.top + PENCIL_Y)
    time.sleep(0.3)


def _prime(win):
    """Arrastre de 'calentamiento' sacrificable.

    En este Paint (WinUI) el PRIMER arrastre despues de tocar un boton del ribbon no pinta.
    Hacemos uno corto y descartable para que el primer trazo real si quede registrado.
    """
    px, py = _to_screen(win, 5, 5)
    pyautogui.moveTo(px, py, duration=0.1)
    pyautogui.dragTo(px + 4, py + 4, duration=0.2, button="left")
    time.sleep(0.1)


def _begin_pencil(win):
    """Prepara el lienzo para dibujar: selecciona lapiz y hace el priming."""
    _select_pencil(win)
    _prime(win)


def _stroke(win, points):
    """Dibuja una polilinea. Cada segmento se traza con dragTo (presiona-arrastra-suelta).

    Importante: en este Paint, arrastrar con mouseDown()+moveTo() suele dejar solo un punto;
    dragTo() por segmento es la tecnica confiable. Los segmentos comparten extremos, asi que
    se ven continuos.
    """
    if len(points) < 2:
        raise ValueError("Se necesitan al menos 2 puntos para dibujar.")
    screen_pts = [_to_screen(win, px, py) for px, py in points]
    for (ax, ay), (bx, by) in zip(screen_pts, screen_pts[1:]):
        pyautogui.moveTo(ax, ay, duration=0.1)
        pyautogui.dragTo(bx, by, duration=DRAW_DURATION, button="left")
        time.sleep(0.1)


# --------------------------------- Tools ------------------------------------

@mcp.tool()
def open_paint() -> str:
    """Abre Paint de Windows (mspaint), lo maximiza y lo enfoca.

    Si ya hay una ventana de Paint abierta, solo la enfoca.
    """
    win = _get_paint_window()
    if win is None:
        subprocess.Popen(["mspaint.exe"])
        # Esperar a que la ventana exista (hasta ~10s).
        for _ in range(40):
            time.sleep(0.25)
            win = _get_paint_window()
            if win is not None:
                break
        if win is None:
            return "Lance mspaint pero no detecte la ventana de Paint a tiempo."
    _focus_paint()
    _canvas_cache.clear()  # re-detectar el lienzo en el estado actual
    left, top, width, height = _canvas_region(_require_paint())
    return (
        f"Paint abierto y enfocado. Region de lienzo (pantalla): "
        f"left={left}, top={top}, width={width}, height={height}."
    )


@mcp.tool()
def get_canvas_info() -> dict:
    """Devuelve la region del lienzo y el estado de la auto-calibracion (diagnostico).

    Informa si el lienzo se detecto por imagen o se uso el fallback de margenes, y donde
    se ubico el icono del Lapiz.
    """
    win = _require_paint()
    detected = _detect_canvas_region(win)
    left, top, width, height = _canvas_region(win)
    pencil = _find_template(PENCIL_TEMPLATE)
    return {
        "window": {"left": win.left, "top": win.top, "width": win.width, "height": win.height},
        "canvas_region_screen": {"left": left, "top": top, "width": width, "height": height},
        "canvas_source": "detected" if detected is not None else "fallback_margins",
        "pencil_source": "image" if pencil is not None else "fallback_coords",
        "pencil_screen": {"x": pencil[0], "y": pencil[1]} if pencil
        else {"x": win.left + PENCIL_X, "y": win.top + PENCIL_Y},
        "cv2_available": cv2 is not None,
    }


@mcp.tool()
def draw_line(x1: int, y1: int, x2: int, y2: int) -> str:
    """Dibuja una linea recta del punto (x1,y1) al (x2,y2). Coordenadas relativas al lienzo."""
    win = _focus_paint()
    _begin_pencil(win)
    _stroke(win, [(x1, y1), (x2, y2)])
    return f"Linea dibujada de ({x1},{y1}) a ({x2},{y2})."


@mcp.tool()
def draw_rectangle(x: int, y: int, width: int, height: int) -> str:
    """Dibuja un rectangulo a mano alzada con esquina superior izquierda en (x,y).

    Se dibuja como 4 lineas conectadas (independiente de la herramienta activa de Paint).
    """
    win = _focus_paint()
    _begin_pencil(win)
    corners = [
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height),
        (x, y),
    ]
    _stroke(win, corners)
    return f"Rectangulo dibujado en ({x},{y}) de {width}x{height}."


@mcp.tool()
def draw_polyline(points: list[list[int]]) -> str:
    """Dibuja un trazo continuo a mano alzada por una lista de puntos [[x,y], [x,y], ...].

    Coordenadas relativas al lienzo. Util para curvas, firmas o formas libres.
    """
    pts = [(p[0], p[1]) for p in points]
    win = _focus_paint()
    _begin_pencil(win)
    _stroke(win, pts)
    return f"Trazo dibujado con {len(pts)} puntos."


@mcp.tool()
def clear_canvas() -> str:
    """Limpia el lienzo (Ctrl+A para seleccionar todo y Delete para borrar).

    Luego reselecciona el Lapiz para descartar la seleccion activa y dejar Paint listo
    para dibujar.
    """
    win = _focus_paint()
    # Asegurar foreground REAL antes de Ctrl+A: un clic en la barra de titulo evita que una
    # notificacion (WhatsApp/Chrome/etc.) robe el foco y el borrado se pierda. Dos pasadas.
    for _ in range(2):
        pyautogui.click(win.left + win.width // 2, win.top + 8)
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.press("delete")
        time.sleep(0.2)
    _select_pencil(win)
    _canvas_cache.clear()  # lienzo limpio: re-detectar region fresca
    return "Lienzo limpiado."


@mcp.tool()
def save_canvas(path: str) -> str:
    """Guarda el contenido del lienzo como PNG en `path`.

    No automatiza el dialogo 'Guardar como' de Paint: toma un screenshot de la region del
    lienzo y lo escribe como PNG (mas confiable).
    """
    win = _focus_paint()
    left, top, width, height = _canvas_region(win)
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    img = pyautogui.screenshot(region=(left, top, width, height))
    img.save(abs_path)
    return f"Lienzo guardado en {abs_path}."


if __name__ == "__main__":
    mcp.run()
