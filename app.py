"""SERVER-4  -  ALL-IN-ONE BACKUP. Does the work of S1 + S2 + S3 (+ fraud checks) when they are down.
It holds the keys of DB1, DB2 and DB3 (same databases, so nothing has to be copied when a failover happens)."""
from common.appfactory import create_app
from modules import auth_mod, profile_mod, wallet_mod, match_mod, lb_mod, geo_mod, ref_mod, history_mod, chat_mod, game_mod, bomb_mod, fraud_mod, sys_mod

app = create_app("s4", "Server-4 (all-in-one backup)",
                 [auth_mod, profile_mod, wallet_mod, match_mod, lb_mod, geo_mod, ref_mod, history_mod, chat_mod, game_mod, bomb_mod, fraud_mod, sys_mod],
                 caps=("wallet", "history", "chat", "fraud"), dbs=("DB1", "DB2", "DB3"))
