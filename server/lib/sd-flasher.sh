# Fichiers de la stack sd-flasher - source par deploy-server.sh. Rien n'est
# jamais ecrase : les images et les reglages sont a l'operateur.
#   images/   images master a flasher (.img, .img.gz, .img.xz, .img.zst)
#   data/     settings.json (Wi-Fi, cles - mode 600) et audit.log, ecrits par l'application
#   keys/     automation.pub, access.pub : copies des cles PUBLIQUES d'automatisation (Ansible)
#             et d'acces (Bastion)
# Necessite ensure_automation_key (appele par setup_ansible_web) et ensure_access_key
# (setup_access_key), tous deux dans automation-key.sh.

setup_sd_flasher() {
    local dir="$SCRIPT_DIR/stacks/sd-flasher" pub="$REPO_ROOT/$AUTOMATION_KEY_REL.pub" access_pub="$REPO_ROOT/$ACCESS_KEY_REL.pub"
    mkdir -p "$dir/images" "$dir/data" "$dir/keys"
    chmod 700 "$dir/data"
    # Uniquement la partie publique : la cle privee ne quitte jamais ansible-web.
    if [ -f "$pub" ]; then
        cp "$pub" "$dir/keys/automation.pub"
    fi
    if [ -f "$access_pub" ]; then
        cp "$access_pub" "$dir/keys/access.pub"
    fi
}
