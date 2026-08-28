#!/bin/zsh
set -e
export QT_QPA_PLATFORM_PLUGIN_PATH="/Users/jiangzhenmin/Applications/TOPAS/OpenTOPAS-install-LET/Frameworks"
export TOPAS_G4_DATA_DIR="/Users/jiangzhenmin/Applications/GEANT4/G4DATA"
export DYLD_LIBRARY_PATH="/Users/jiangzhenmin/Applications/TOPAS/OpenTOPAS-install-LET/Frameworks:/Users/jiangzhenmin/Applications/TOPAS/OpenTOPAS-install-LET/lib:/Users/jiangzhenmin/Applications/GEANT4/geant4-install/lib:${DYLD_LIBRARY_PATH:-}"
exec "/Users/jiangzhenmin/Applications/TOPAS/OpenTOPAS-install-LET/bin/topas" "$@"
