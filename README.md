Oui, bien sûr — voici le schéma en Markdown avec une police fixe :

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