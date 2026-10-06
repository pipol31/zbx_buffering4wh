# Buffering for Zabbix WebHook
Il s'agit d'ajouter une fonctionnalité plus robuste que le simple retry à un WebHook Zabbix. Si le WebHook échoue dans sa tentative de notification au sens large, alors il se rabat sur un service local qui bufferize les données et qui possède un mécanisme de rejeu.
Ici on part sur une notification via un POST de payload JSON qui s'il échoue est conservé dans un fichier JSON avec des tentatives de réémissions (backoff expo) et qui loggue si au final le endpoint reste indisponible ou en erreur.
Le mécanisme (écoute / réémission) sera lancé via systemctl.

(Vibe Codé avec Claude)

```text
┌─────────────────────────────────────────┐
│ Zabbix MediaType Webhook (script JS)    │
│                                         │
│ 1. Tente appel direct vers VIP          │
│ 2. Analyse succès/échec elle-même       │
│    (timeout, erreur réseau, 5xx...)     │
└───────────────┬─────────────────────────┘
                │
     ┌──────────┴──────────┐
     │                     │
   Succès                Échec
     │                     │
     ▼                     ▼
                         POST http://127.0.0.1:9191/buffer
                         { payload original, endpoint,
                           timestamp, raison échec }
                           │
                           ▼
                ┌────────────────────┐
                │ Relay (buffer)     │
                │ écrit queue.jsonl  │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ Worker de rejeu    │
                │ (backoff expo)     │
                │ rejoue vers VIP    │
                └─────────┬──────────┘
                          │        │
                        Succès   Échec définitif (4xx)
                          │        │
                          ▼        ▼
                                retire de la queue dead-letter.jsonl
```


# Déclaration sous forme de service systemd
```
[Unit]
Description=Zabbix Webhook Relay (buffer + rejeu)
After=network.target

[Service]
Type=simple
User=zbx-relay
Group=zbx-relay
ExecStart=/usr/bin/python3 /opt/zbx-relay/relay.py
Restart=always
RestartSec=5
WorkingDirectory=/opt/zbx-relay

# Durcissement
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/zbx-relay /var/log/zbx-relay
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

## Installation
```
# Utilisateur dédié
sudo useradd --system --no-create-home --shell /usr/sbin/nologin zbx-relay

# Arborescence
sudo mkdir -p /opt/zbx-relay /var/lib/zbx-relay /var/log/zbx-relay
sudo cp relay.py /opt/zbx-relay/
sudo chown -R zbx-relay:zbx-relay /opt/zbx-relay /var/lib/zbx-relay /var/log/zbx-relay

# Dépendance
sudo pip3 install requests

# Service
sudo cp zbx-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zbx-relay
sudo systemctl status zbx-relay

# Vérification des logs
sudo tail -f /var/log/zbx-relay/relay.log
```
