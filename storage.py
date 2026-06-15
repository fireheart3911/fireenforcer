import json

storage: dict = {}
count: int = -22767
# Guards against persisting before the on-disk data has been read in. Writing
# the empty startup dict before load_data() would clobber data.json.
_loaded: bool = False


def load_data():
    global storage, count, _loaded
    try:
        with open("data.json", "r", encoding="utf-8") as f:
            storage = json.load(f)
            _loaded = True
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
        _loaded = True
        print("No existing data found, starting fresh.")
    except json.JSONDecodeError as e:
        # Corrupt file — do NOT reset to empty (that would let a later save wipe
        # a potentially recoverable file). Stay unloaded so save_data() refuses.
        print(f"\033[91m[Storage] data.json is corrupt ({e}); NOT loading and "
              f"refusing to overwrite it. Fix or restore the file.\033[0m")


def save_data():
    if not _loaded:
        print("\033[91m[Storage] save_data() called before load_data() succeeded; "
              "refusing to write to avoid clobbering data.json.\033[0m")
        return
    storage["id_count"] = count
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(storage, f, indent=4, ensure_ascii=False)
    print("\033[95m[Storage] Data saved to file.\033[0m")


def next_id() -> int:
    """Increment and return the global ID counter."""
    global count
    count += 1
    return count