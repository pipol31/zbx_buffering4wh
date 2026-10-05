┌─────────────────────────────────────────┐
│ Zabbix MediaType Webhook (script JS)    │
│                                         │
│ 1. Tente appel direct vers VIP          │
│ 2. Analyse succès/échec elle-même       │
│ (timeout, erreur réseau, 5xx...)        │
└───────────────┬─────────────────────────┘
                │
     ┌──────────┴──────────┐
     │                     │
   Succès                Échec
     │                     │
     ▼                     ▼
                 Fin POST http://127.0.0.1:9191/buffer
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
