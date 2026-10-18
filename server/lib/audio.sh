# Fichiers de la stack audio-admin - source par deploy-server.sh. data/ : state.json (niveaux, presets,
# machines, ecrit par l'application) et agent.token, le jeton que les agents audio des machines
# presentent au serveur. Jamais ecrase : regenerer le jeton couperait toutes les machines deja installees.

setup_audio() {
    local dir="$SCRIPT_DIR/stacks/audio-admin/data" token
    mkdir -p "$dir"
    if [ ! -s "$dir/agent.token" ]; then
        token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
        (umask 077 && printf '%s\n' "$token" > "$dir/agent.token")
        echo "[+] Jeton des agents audio genere (server/stacks/audio-admin/data/agent.token, non versionne)."
    fi
    chmod 600 "$dir/agent.token"
}
