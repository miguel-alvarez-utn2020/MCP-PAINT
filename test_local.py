"""Prueba manual del servidor sin pasar por Claude.

Abre Paint, dibuja un rectangulo, una linea diagonal y un trazo libre, y guarda salida.png.
Ejecutar:  python test_local.py

OJO: durante la prueba el script toma control del mouse. No uses la PC mientras corre.
Para abortar de emergencia, mueve el mouse a una esquina de la pantalla (FAILSAFE).
"""

import time

import server

if __name__ == "__main__":
    print(server.open_paint())
    time.sleep(0.5)

    print(server.get_canvas_info())

    print(server.clear_canvas())
    time.sleep(0.3)

    print(server.draw_rectangle(100, 100, 300, 200))
    print(server.draw_line(100, 100, 400, 300))
    print(server.draw_polyline([[450, 120], [500, 180], [550, 120], [600, 180], [650, 120]]))

    print(server.save_canvas("salida.png"))
    print("Listo. Revisa salida.png")
