#!/usr/bin/env bash
# Сборка установщика PrintHub для Windows на Linux (Ubuntu/Debian).
#   sudo apt-get install -y nsis gcc-mingw-w64-x86-64 binutils-mingw-w64-x86-64 unzip curl
#   pip install pillow
#   UPDATE_REPO=user/printhub ./packaging/build_windows.sh 0.2.0
# Результат: dist/PrintHub-Setup-<версия>.exe и .sha256 рядом.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT/packaging"
VERSION="${1:-$(cd "$ROOT" && python3 -c 'import printhub; print(printhub.__version__)')}"
VERSION="${VERSION#v}"
PYVER="${PYVER:-3.13.12}"
PYTAG="$(echo "$PYVER" | awk -F. '{print $1 $2}')"
PYMINOR="$(echo "$PYVER" | awk -F. '{print $1 "." $2}')"
OUT="$ROOT/dist"
BUILD="$ROOT/build/windows"
CACHE="$ROOT/build/cache"
STAGE="$BUILD/PrintHub"
UPDATE_REPO="${UPDATE_REPO:-}"
UPDATE_URL="${UPDATE_URL:-}"

echo "==> PrintHub $VERSION, Python $PYVER"
case "$BUILD" in */build/windows) rm -rf -- "$BUILD" ;; *) echo "неожиданный путь сборки: $BUILD"; exit 1 ;; esac
mkdir -p "$STAGE/python" "$STAGE/app" "$OUT" "$CACHE"

# 1. Встроенный Python (официальный embeddable-пакет python.org)
EMBED="$CACHE/python-$PYVER-embed-amd64.zip"
[ -f "$EMBED" ] || curl -fsSL -o "$EMBED" "https://www.python.org/ftp/python/$PYVER/python-$PYVER-embed-amd64.zip"
unzip -q -o "$EMBED" -d "$STAGE/python"
printf 'python%s.zip\r\n.\r\nLib\\site-packages\r\n..\\app\r\nimport site\r\n' "$PYTAG" > "$STAGE/python/python${PYTAG}._pth"

# 2. Зависимости — готовые колёса для Windows x64
python3 -m pip install --quiet --disable-pip-version-check --only-binary=:all: \
  --platform win_amd64 --python-version "$PYMINOR" --implementation cp \
  --target "$STAGE/python/Lib/site-packages" -r "$PKG/requirements-win.txt"
find "$STAGE/python/Lib/site-packages" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$STAGE/python/Lib/site-packages" -path "*/bin/*.exe" -delete 2>/dev/null || true

# 3. Код приложения
(cd "$ROOT" && tar --exclude="__pycache__" --exclude="*.pyc" -cf - printhub) | tar -xf - -C "$STAGE/app"
cat > "$STAGE/app/printhub/build_info.py" <<EOF
"""Параметры сборки (создано build_windows.sh)."""
UPDATE_REPO = "$UPDATE_REPO"
UPDATE_URL = "$UPDATE_URL"
EOF
printf '__version__ = "%s"\n' "$VERSION" > "$STAGE/app/printhub/__init__.py"

# 4. Значок и лаунчер PrintHub.exe
# готовый значок SEBE 3D из репозитория (на сборочном сервере нет шрифта Inter); если его нет — рисуем
if [ -f "$PKG/sebe3d.ico" ]; then cp "$PKG/sebe3d.ico" "$BUILD/printhub.ico"; else python3 "$PKG/make_icon.py" "$BUILD/printhub.ico"; fi
cp "$BUILD/printhub.ico" "$STAGE/printhub.ico"
IFS=. read -r V1 V2 V3 _ <<<"$VERSION.0.0"
sed -e "s/@V1@/$V1/g; s/@V2@/$V2/g; s/@V3@/${V3:-0}/g; s/@VERSION@/$VERSION/g" "$PKG/launcher.rc.in" > "$BUILD/launcher.rc"
(cd "$BUILD" && x86_64-w64-mingw32-windres launcher.rc -O coff -o launcher.res)
x86_64-w64-mingw32-gcc -O2 -municode -mwindows -s -o "$STAGE/PrintHub.exe" "$PKG/launcher.c" "$BUILD/launcher.res"

# 5. Установщик (NSIS читает .nsi как UTF-8 только при наличии BOM)
printf '\xef\xbb\xbf' > "$BUILD/installer.nsi"
sed 's/^\xef\xbb\xbf//' "$PKG/installer.nsi" >> "$BUILD/installer.nsi"
SETUP="PrintHub-Setup-$VERSION.exe"
(cd "$BUILD" && makensis -V2 -DVERSION="$VERSION" -DSRCDIR="$STAGE" -DOUTFILE="$OUT/$SETUP" installer.nsi)

(cd "$OUT" && sha256sum "$SETUP" > "$SETUP.sha256")
echo "==> Готово: $OUT/$SETUP ($(du -h "$OUT/$SETUP" | cut -f1))"
cat "$OUT/$SETUP.sha256"
