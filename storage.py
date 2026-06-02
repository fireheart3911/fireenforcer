import json

storage: dict = {}
count: int = -22767


def load_data():
    global storage, count
    try:
        with open("data.json", "r", encoding="utf-8") as f:
            storage = json.load(f)
            count = storage.get("id_count", count)

            # Ensure all top-level keys exist
            for key in ("tickets", "status_message", "user_statuses", "elo_players", "elo_sessions", "elo_types", "vacations", "user_prefs", "queue_stop_thread", "votes", "vote_blocks", "alt_links"):
                storage.setdefault(key, {})

            # Migrate old flat elo_players format → nested per-type format
            players = storage["elo_players"]
            if players and "discord_id" in next(iter(players.values()), {}):
                migrated = {}
                for user_id, data in players.items():
                    migrated[user_id] = {"default": data}
                storage["elo_players"] = migrated
                print("Migrated Elo players to new multi-elo format")

            # Migrate old single-dict vacations → per-user list of vacations
            vac_migrated = False
            for user_id, v in list(storage["vacations"].items()):
                if isinstance(v, dict):
                    storage["vacations"][user_id] = [{
                        "id": f"VAC-legacy-{user_id}",
                        "start_at": v.get("start_at", 0),
                        "end_at": v.get("end_at", 0),
                        "destination": v.get("destination", ""),
                        # field renames: availability→reachability, timezone→tz_note
                        "reachability": v.get("reachability", v.get("availability", "")),
                        "tz_note": v.get("tz_note", v.get("timezone", "")),
                        "created_at": v.get("created_at", ""),
                    }]
                    vac_migrated = True
            if vac_migrated:
                print("Migrated vacations to new multi-vacation list format")

            # Prune finished elo sessions — terminal states are kept only in logs.
            terminal = ("cancelled", "denied", "verified", "expired")
            stale = [sid for sid, s in storage["elo_sessions"].items()
                     if s.get("status") in terminal]
            for sid in stale:
                del storage["elo_sessions"][sid]
            if stale:
                print(f"Pruned {len(stale)} finished elo session(s)")

            print(f"Loaded data. Current count: {count}")

            # If load-time cleanup/migrations changed anything, flush to disk now.
            if stale or vac_migrated:
                save_data()

    except FileNotFoundError:
        storage = {
            "tickets": {},
            "status_message": {},
            "user_statuses": {},
            "elo_players": {},
            "elo_sessions": {},
            "elo_types": {},
            "vacations": {},
            "user_prefs": {},
        }
        print("No existing data found, starting fresh.")


def save_data():
    storage["id_count"] = count
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(storage, f, indent=4, ensure_ascii=False)
    print("Successfully saved data to local storage.")


def next_id() -> int:
    """Increment and return the global ID counter."""
    global count
    count += 1
    return count