# Paint MCP — controla Paint de Windows desde Claude

Servidor **MCP** local que le permite a Claude (vía Claude Code) abrir **Paint de
Windows 11** y dibujar en el lienzo. Paint no tiene API, así que se controla por
**automatización de GUI** (`pyautogui` mueve el mouse en coordenadas de pantalla).

## Requisitos

- Windows 10/11 con Paint (`mspaint.exe`)
- Python 3.10+ (probado en 3.14)

## Instalación

```powershell
python -m pip install -r requirements.txt
```

Dependencias: `mcp[cli]`, `pyautogui`, `pillow`, `opencv-python`, `numpy` (`pygetwindow` y
`pywin32` entran como dependencias en Windows). `opencv-python` y `numpy` se usan para la
auto-calibración (detección del lienzo y del ícono del lápiz por imagen).

## Cómo funciona

Las coordenadas de las tools son **relativas al lienzo**: `(0,0)` es la esquina superior
izquierda del área dibujable. El servidor las traduce a pantalla usando la posición de la
ventana de Paint + unos **márgenes de calibración** que descuentan el panel de herramientas
izquierdo, el ribbon superior, los bordes y la barra de estado.

Detalles que hicieron falta para que el dibujo funcione en el Paint de Windows 11 (WinUI):

1. **Match por proceso, no por título.** La ventana de Paint se ubica buscando el proceso
   `mspaint.exe`. Buscar por título ("paint") fallaba porque VS Code abierto en la carpeta
   `MCP-TEST-Paint` también contiene "paint" en su título.
2. **Selección del lápiz.** Antes de dibujar se clickea el ícono del Lápiz en el ribbon.
   Esto además descarta cualquier selección activa (p. ej. la que deja `Ctrl+A` al limpiar),
   que si no haría que los arrastres *muevan la selección* en vez de dibujar.
3. **Priming.** El **primer** arrastre después de tocar un botón del ribbon no pinta, así que
   se hace un arrastre corto sacrificable antes del trazo real.
4. **`dragTo` por segmento.** Arrastrar con `mouseDown()`+`moveTo()` solía dejar solo un
   punto; cada segmento se traza con `dragTo()` (presiona-arrastra-suelta). Los segmentos
   comparten extremos, así que se ven continuos.

## Auto-calibración

Para no depender de coordenadas fijas (que se rompen al cambiar de monitor, resolución o
escala), el servidor se auto-calibra en cada uso:

- **Lienzo:** se detecta la región dibujable buscando la **banda blanca dominante** dentro
  de la ventana (numpy). El resultado se cachea por geometría de ventana.
- **Lápiz:** se ubica el ícono por **reconocimiento de imagen** (`cv2.matchTemplate` sobre
  `assets/pencil.png`), tomando la mejor coincidencia en pantalla.

Si la detección falla (p. ej. sin `opencv`/`numpy`), cae automáticamente a los márgenes y
coordenadas fijas configurables por entorno (ver más abajo). `get_canvas_info()` informa si
está usando `detected` o `fallback`.

> Nota: la plantilla `assets/pencil.png` y la detección asumen **100 % de escala** de
> pantalla. Con otra escala (125 %, 150 %…) el ícono cambia de tamaño y puede no matchear;
> en ese caso se usa el fallback de coordenadas.

## Tools disponibles

| Tool | Descripción |
|------|-------------|
| `open_paint()` | Abre `mspaint`, lo maximiza y lo enfoca (si ya está abierto, solo lo enfoca). |
| `get_canvas_info()` | Devuelve bounds de la ventana y región del lienzo (para depurar/calibrar). |
| `draw_line(x1,y1,x2,y2)` | Línea recta. |
| `draw_rectangle(x,y,width,height)` | Rectángulo (4 líneas conectadas). |
| `draw_polyline(points)` | Trazo libre por una lista `[[x,y],...]`. |
| `clear_canvas()` | Limpia el lienzo (Ctrl+A + Delete). |
| `save_canvas(path)` | Guarda la región del lienzo como PNG (screenshot, sin usar el diálogo de Paint). |

## Probar sin Claude

```powershell
python test_local.py
```

Abre Paint, dibuja un rectángulo, una línea y un trazo libre, y guarda `salida.png`.

> ⚠️ Durante la ejecución el script **toma control del mouse**. No uses la PC mientras
> corre. Para abortar de emergencia, mueve el mouse a una **esquina** de la pantalla
> (FAILSAFE de pyautogui).

## Calibración (fallback)

Normalmente **no hace falta calibrar**: la auto-detección de arriba se encarga. Estos valores
son el **fallback** que se usa solo si la detección por imagen falla (sin `opencv`/`numpy`, o
escala ≠ 100 %). Se ajustan por variables de entorno (no hace falta tocar el código):

| Variable | Default | Qué controla |
|----------|---------|--------------|
| `PAINT_MARGIN_TOP` | 218 | Barra de título + ribbon superior |
| `PAINT_MARGIN_LEFT` | 277 | Borde + panel de herramientas izquierdo |
| `PAINT_MARGIN_RIGHT` | 285 | Borde/panel derecho |
| `PAINT_MARGIN_BOTTOM` | 135 | Barra de estado inferior |
| `PAINT_PENCIL_X` | 262 | X del ícono Lápiz (relativa a la ventana) |
| `PAINT_PENCIL_Y` | 87 | Y del ícono Lápiz (relativa a la ventana) |
| `PAINT_DRAW_DURATION` | 0.4 | Segundos por segmento (subir si Paint no registra el trazo) |

**Cómo recalibrar:** llamá `get_canvas_info()` para ver la región del lienzo y la posición
calculada del lápiz, o sacá un screenshot de pantalla completa y medí: la esquina superior
izquierda del lienzo blanco te da `MARGIN_LEFT`/`MARGIN_TOP`, y el ícono del Lápiz en el
ribbon te da `PAINT_PENCIL_X`/`PAINT_PENCIL_Y`.

## Usar desde Claude Code

El archivo `.mcp.json` ya registra el servidor (scope de proyecto). Para activarlo:

1. Reinicia Claude Code en este directorio para que cargue `.mcp.json`.
2. Verifica con `/mcp` que aparece el servidor **paint** (aprueba el servidor si te lo pide).
3. Pídele a Claude algo como: *"abre Paint y dibuja un cuadrado"*.

## Límites del POC

- Dibuja siempre con el **Lápiz** y color negro. Cambiar color / grosor / pincel / texto
  **no** está incluido (requiere clickear más botones del ribbon). Sería una v2.
- La calibración (márgenes y posición del lápiz) depende de la **resolución y escala** de
  pantalla. Los defaults son para 2560×1080 al 100%; en otra config hay que ajustar las
  variables de entorno de arriba.
