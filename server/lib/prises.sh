# Fichiers de la stack prises-admin - source par deploy-server.sh. data/ : association
# prise -> module et historique de consommation (jamais ecrase).

setup_prises() {
    mkdir -p "$SCRIPT_DIR/stacks/prises-admin/data"
}
