#!/usr/bin/env bash
# Instala DictaVoz en el menú de aplicaciones y en ~/.local/bin.
# No requiere root. Para desinstalar, borra los dos ficheros que crea.
set -euo pipefail

APP_DIR="$(dirname "$(readlink -f "$0")")"
BIN_DIR="${HOME}/.local/bin"
APPS_DIR="${HOME}/.local/share/applications"
LAYOUT="latam"

echo "Carpeta de DictaVoz: ${APP_DIR}"

# Asegura que existe el mapa de teclado para el layout indicado.
if [ ! -f "${APP_DIR}/dictavoz/keymaps/${LAYOUT}.json" ]; then
  echo "Generando mapa de teclado '${LAYOUT}'…"
  python3 "${APP_DIR}/tools/gen_keymap.py" --layout "${LAYOUT}"
fi

mkdir -p "${BIN_DIR}" "${APPS_DIR}"

# Icono de la aplicación (SVG + PNG para lanzadores clásicos).
SVG_ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"
PNG_ICON_DIR="${HOME}/.local/share/icons/hicolor/256x256/apps"
mkdir -p "${SVG_ICON_DIR}" "${PNG_ICON_DIR}"
cp "${APP_DIR}/icons/dictavoz.svg" "${SVG_ICON_DIR}/dictavoz.svg"
if [ -f "${APP_DIR}/icons/dictavoz.png" ]; then
  cp "${APP_DIR}/icons/dictavoz.png" "${PNG_ICON_DIR}/dictavoz.png"
fi
gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true

# Lanzador de línea de comandos.
cat > "${BIN_DIR}/dictavoz" <<EOF
#!/usr/bin/env bash
cd "${APP_DIR}"
exec python3 -m dictavoz "\$@"
EOF
chmod +x "${BIN_DIR}/dictavoz"

# Entrada del menú de aplicaciones (usa run.sh).
cat > "${APPS_DIR}/dictavoz.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DictaVoz
Comment=Dictado por voz que escribe donde tengas el cursor
Exec=${APP_DIR}/run.sh
Icon=dictavoz
Terminal=false
Categories=Utility;Accessibility;
Keywords=voz;dictado;speech;texto;
EOF

echo "Instalado:"
echo "  ${BIN_DIR}/dictavoz"
echo "  ${APPS_DIR}/dictavoz.desktop"
echo "  iconos en ~/.local/share/icons/hicolor"
echo
echo "Si 'dictavoz' no se encuentra en el terminal, añade ~/.local/bin al PATH."
echo "Para desinstalar: rm '${BIN_DIR}/dictavoz' '${APPS_DIR}/dictavoz.desktop' "
echo "  '${SVG_ICON_DIR}/dictavoz.svg' '${PNG_ICON_DIR}/dictavoz.png'"
