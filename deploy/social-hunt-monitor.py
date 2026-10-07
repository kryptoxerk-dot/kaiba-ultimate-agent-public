"""Hermes cron monitor: stable pending IDs; no model run when the inbox is unchanged."""
import json
import sys
from pathlib import Path

root=Path('/home/kaiba/kaiba')
sys.path.insert(0,str(root))
from kaiba.hunters.social_hunt import Store  # noqa: E402

store=Store(root/'data/social-hunt.db')
rows=store.query('SELECT s.tweet_id,s.lane FROM signals s LEFT JOIN reviews r ON r.tweet_id=s.tweet_id AND r.lane=s.lane WHERE r.reviewed_ms IS NULL ORDER BY s.lane,s.tweet_id LIMIT 50')
print(json.dumps(rows,sort_keys=True,separators=(',',':')))
