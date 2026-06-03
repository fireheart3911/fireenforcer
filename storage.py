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

            # Prune finished elo sessions — terminal states are kept only in logs.
            terminal = ("cancelled", "denied", "verified", "expired")
            stale = [sid for sid, s in storage["elo_sessions"].items()
                     if s.get("status") in terminal]
            for sid in stale:
                del storage["elo_sessions"][sid]
            if stale:
                print(f"Pruned {len(stale)} finished elo session(s)")

            print(f"Loaded data. Current count: {count}")

            # If load-time cleanup changed anything, flush to disk now.
            if stale:
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
    print("\033[95m[Storage] Data saved to file.\033[0m")


def next_id() -> int:
    """Increment and return the global ID counter."""
    global count
    count += 1
    return count