# User-configured TOPAS server connection

`ssh_server.json` stores non-secret infrastructure settings, not per-patient
data. The GUI may update it. It never stores a password or private-key contents.
Authentication is delegated to OpenSSH (ssh-agent/macOS Keychain), or to an
existing private-key file whose path is selected by the user.

Commission a connection in this order:

1. Enter a direct hostname/IP plus username, or select the OpenSSH-alias mode and
   enter an alias from `~/.ssh/config`.
2. Choose ssh-agent/Keychain authentication or select an existing private key.
   If the key has a passphrase, load it into the agent before testing.
3. Set absolute server paths for the TOPAS executable, Geant4 environment setup
   script, Geant4 data directory, and remote job root, then save.
4. Click **Inspect host key**. Independently verify the displayed SHA-256
   fingerprint with the server administrator before clicking **Trust verified
   key**. The exact key is then written to `ssh_known_hosts`.
5. Enable the server, run **Test connection**, and then **Check TOPAS + Geant4**.
   Do not send patient CT until every blocking check is resolved and
   institutional data-transfer approval exists.

Changing the hostname, alias or port clears the pinned fingerprint. Replacing an
existing key requires a separate explicit warning/confirmation. Historical
remote bundles are immutable and keep their original server audit information.

The project uploads generated TOPAS parameter files and CT input only. It never
uploads the local TOPAS or Geant4 executable. The generated remote launcher
sources the selected server environment and executes the selected server TOPAS
binary. Browser-entered arbitrary shell commands are never accepted.
