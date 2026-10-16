# Fichiers de la stack matter (controleur des prises connectees) - source par
# deploy-server.sh. data/ : etat Matter (fabric, prises approvisionnees) - jamais
# ecrase, a sauvegarder.

setup_matter() {
    mkdir -p "$SCRIPT_DIR/stacks/matter/data"
    chmod 700 "$SCRIPT_DIR/stacks/matter/data"
}
