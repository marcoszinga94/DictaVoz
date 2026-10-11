"""DictaVoz: dictado por voz que escribe en la posición del cursor.

Convierte el habla capturada por el micrófono en texto y lo escribe donde
tengas el cursor, usando un teclado virtual por /dev/uinput (funciona en
Wayland y X11) y whisper.cpp local para el reconocimiento de voz.
"""

__version__ = "0.1.0"
