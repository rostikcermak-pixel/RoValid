#!/usr/bin/env python3
"""How good is a username, not just whether it is free.

Free names are not scarce. A one-minute proxyless sample found 158 free
five-character names and not one of them was digit-free - they were all
things like 6aw42 and p0xmt. What is scarce is a name anyone would want, so
the site ranks by desirability and lets the junk sink.

Scoring is structural rather than a dictionary lookup, apart from a small
list of outright jackpots. A structural rule keeps working on names no word
list contains - zurik and mavlo read as names because of their shape, not
because they mean anything.
"""

from __future__ import annotations

VOWELS = set("aeiou")
LETTERS = set("abcdefghijklmnopqrstuvwxyz")

# Short, common, and instantly readable. Kept deliberately small: this is the
# "stop scrolling" tier, and padding it with obscure words would cheapen it.
WORDS = {
    # 3
    "ace", "air", "ash", "axe", "bat", "bee", "bio", "bit", "box", "boy",
    "bug", "cat", "cow", "cry", "cup", "dog", "dot", "dry", "ear", "eat",
    "egg", "elf", "end", "eye", "fan", "fig", "fin", "fix", "fly", "fog",
    "fox", "fun", "gas", "gem", "god", "gun", "guy", "hat", "hit", "hot",
    "ice", "ink", "jam", "jet", "job", "joy", "key", "kid", "law", "leg",
    "lie", "log", "low", "mad", "man", "map", "mix", "mud", "net", "new",
    "nut", "oak", "odd", "oil", "one", "owl", "pen", "pet", "pig", "pin",
    "pop", "pot", "ram", "rat", "raw", "red", "rib", "rip", "rob", "run",
    "sad", "sea", "sky", "sun", "tag", "tap", "tax", "tea", "ten", "tip",
    "toe", "top", "toy", "van", "war", "wax", "web", "wet", "who", "win",
    "yes", "zap", "zen", "zip", "zoo",
    # 4
    "acid", "atom", "aura", "bank", "bass", "beam", "bear", "beat", "bell",
    "bird", "bite", "blue", "boom", "bolt", "bone", "book", "boss", "brew",
    "buzz", "calm", "cape", "cash", "cast", "cell", "chip", "city", "claw",
    "clay", "code", "coin", "cold", "cook", "cool", "core", "crow", "cube",
    "dark", "dawn", "deep", "demo", "dice", "dirt", "dive", "dome", "doom",
    "door", "dove", "down", "drum", "dusk", "dust", "east", "echo", "edge",
    "epic", "evil", "exit", "face", "fade", "fair", "fall", "fame", "fang",
    "fast", "fate", "fear", "fire", "fish", "fist", "flag", "flow", "foam",
    "fold", "font", "form", "fort", "free", "frog", "fuel", "fury", "game",
    "gate", "gaze", "gear", "gift", "girl", "glow", "goat", "gold", "gone",
    "grim", "grow", "hail", "half", "halo", "hard", "hawk", "haze", "heal",
    "heat", "helm", "hero", "hive", "hold", "hole", "home", "hope", "horn",
    "host", "hunt", "hurt", "icon", "idea", "idol", "iron", "jade", "jazz",
    "join", "jump", "keen", "kick", "kind", "king", "kiss", "kite", "knot",
    "lace", "lake", "lamb", "lamp", "land", "lane", "lava", "lead", "leaf",
    "leap", "lens", "life", "lift", "lime", "line", "link", "lion", "live",
    "lock", "loop", "lord", "lost", "loud", "luck", "lung", "lure", "mage",
    "mail", "main", "mark", "mask", "mass", "maze", "mind", "mint", "mist",
    "moon", "moss", "moth", "muse", "myth", "nail", "name", "navy", "neon",
    "nest", "news", "next", "nice", "node", "none", "noon", "norm", "note",
    "nova", "oath", "onyx", "opal", "open", "oval", "pace", "pack", "page",
    "pain", "pale", "palm", "park", "path", "peak", "pear", "pine",
    "pink", "plan", "play", "plot", "plus", "poem", "pole", "pond", "pony",
    "pool", "pore", "port", "pose", "pull", "pure", "push", "quiz", "race",
    "rage", "rail", "rain", "rank", "rare", "rate", "rave", "real",
    "reef", "rest", "rich", "ride", "ring", "riot", "rise", "risk", "road",
    "roar", "robe", "rock", "role", "roll", "roof", "room", "root", "rope",
    "rose", "ruby", "rule", "rush", "rust", "sage", "sail", "salt", "sand",
    "save", "scar", "seal", "seed", "self", "sell", "shot", "show", "sign",
    "silk", "sing", "sink", "site", "size", "skin", "slam", "slip", "slow",
    "snap", "snow", "soft", "soil", "sold", "solo", "song", "soul", "soup",
    "sour", "spin", "star", "stay", "stem", "step", "stop", "sung", "surf",
    "swim", "tale", "talk", "tall", "tank", "tape", "task", "team", "tear",
    "tech", "tell", "tent", "term", "test", "text", "thin", "tide", "tile",
    "time", "tiny", "toll", "tomb", "tone", "tool", "torn", "tour", "town",
    "trap", "tree", "trim", "trip", "true", "tube", "tune", "turn", "twin",
    "type", "unit", "vale", "vain", "vase", "vast", "veil", "vein", "verb",
    "vibe", "view", "vine", "void", "volt", "vote", "wage", "wait", "wake",
    "walk", "wall", "wand", "want", "ward", "warm", "warp", "wash", "wave",
    "wear", "west", "wild", "will", "wind", "wine", "wing", "wire", "wise",
    "wish", "wolf", "wood", "wool", "word", "wore", "work", "worm", "yard",
    "yarn", "year", "yoga", "zero", "zone", "zoom",
    # 5
    "abyss", "adept", "agile", "alarm", "album", "alien", "alloy", "amber",
    "angel", "anger", "apple", "arena", "armor", "arrow", "ashen", "aspen",
    "atlas", "audio", "avian", "azure", "badge", "bacon", "baron", "beach",
    "beast", "began", "bench", "berry", "birch", "black", "blade", "blame",
    "blast", "blaze", "blend", "blind", "bliss", "block", "bloom", "blues",
    "blunt", "board", "boost", "brain", "brand", "brave", "bread", "break",
    "brick", "brief", "bring", "brisk", "broad", "brook", "brush", "brute",
    "build", "burst", "cabin", "cable", "candy", "canon", "cargo", "carve",
    "catch", "cause", "cedar", "chain", "chalk", "charm", "chase", "cheap",
    "cheer", "chess", "chief", "chill", "china", "chose", "civic", "claim",
    "clash", "class", "clean", "clear", "click", "cliff", "climb", "cloak",
    "clock", "close", "cloud", "coast", "cobra", "colon", "comet", "coral",
    "count", "court", "cover", "crack", "craft", "crane", "crash", "crazy",
    "cream", "creed", "creek", "crest", "crime", "crisp", "cross", "crowd",
    "crown", "crude", "crush", "curve", "cycle", "daily", "dance", "dandy",
    "dealt", "death", "debut", "decay", "delta", "demon", "dense", "depth",
    "devil", "diary", "digit", "dirty", "ditch", "diver", "dizzy", "donor",
    "doubt", "draft", "drain", "drake", "drama", "drank", "dream", "dress",
    "drift", "drill", "drink", "drive", "drone", "drown", "druid", "dryad",
    "dusty", "dwarf", "eagle", "early", "earth", "eight", "elder", "elite",
    "ember", "empty", "enemy", "enjoy", "enter", "envoy", "equal", "error",
    "essay", "event", "every", "exact", "exile", "exist", "extra", "fable",
    "faint", "fairy", "faith", "false", "fancy", "fatal", "fault", "favor",
    "feast", "fence", "ferry", "fever", "fiber", "field", "fiend",
    "fifth", "fight", "final", "flame", "flare", "flash", "fleet", "flesh",
    "flint", "float", "flood", "floor", "flora", "flour", "fluid", "flute",
    "focus", "force", "forge", "forth", "found", "frame", "fraud", "fresh",
    "front", "frost", "fruit", "fuzzy", "ghost", "giant", "given", "glare",
    "glass", "gleam", "globe", "gloom", "glory", "glove", "grace", "grade",
    "grain", "grand", "grant", "grape", "grasp", "grass", "grave", "great",
    "greed", "green", "greet", "grief", "grill", "grind", "gripe", "groan",
    "groom", "gross", "group", "grove", "guard", "guess", "guest", "guide",
    "guild", "habit", "happy", "harsh", "haste", "haunt", "haven", "havoc",
    "heart", "heavy", "hedge", "hello", "hence", "hertz", "hinge", "hobby",
    "honey", "honor", "horde", "horse", "hotel", "hound", "house", "human",
    "humor", "hurry", "hyena", "ideal", "image", "imply", "index", "inner",
    "input", "irony", "issue", "ivory", "jelly", "jewel", "joint", "jolly",
    "judge", "juice", "karma", "kneel", "knife", "knock", "known", "koala",
    "label", "labor", "lance", "large", "laser", "later", "laugh", "layer",
    "learn", "lease", "least", "leave", "legal", "lemon", "level", "lever",
    "light", "limit", "linen", "liver", "lobby", "local", "lodge", "logic",
    "loose", "lotus", "lower", "loyal", "lucid", "lucky", "lunar", "lunch",
    "lyric", "magic", "major", "maple", "march", "match", "maybe", "mayor",
    "medal", "media", "medic", "melon", "mercy", "merge", "merit", "merry",
    "metal", "meter", "midst", "might", "minor", "minus", "mirth", "model",
    "moist", "money", "month", "moral", "motor", "mound", "mount", "mouse",
    "mouth", "movie", "mural", "music", "naive", "nasty", "naval", "needy",
    "nerve", "never", "newer", "night", "ninja", "noble", "noise", "north",
    "novel", "nurse", "nylon", "oasis", "occur", "ocean", "offer", "olive",
    "omega", "onion", "opera", "orbit", "order", "organ", "otter", "outer",
    "owner", "ozone", "paint", "panel", "panic", "paper", "party", "pasta",
    "patch", "pause", "peace", "peach", "pearl", "pedal", "penny", "perch",
    "peril", "phase", "phone", "photo", "piano", "piece", "pilot", "pinch",
    "pitch", "pivot", "pixel", "pizza", "place", "plain", "plane", "plant",
    "plate", "plaza", "plumb", "point", "polar", "porch", "pound", "power",
    "press", "pride", "prime", "print", "prior", "prism", "prize", "probe",
    "prone", "proof", "proud", "prove", "proxy", "pulse", "punch", "pupil",
    "puppy", "purge", "quake", "queen", "query", "quest", "queue", "quick",
    "quiet", "quill", "quirk", "quota", "quote", "radar", "radio", "raise",
    "rally", "ranch", "range", "rapid", "ratio", "raven", "reach", "react",
    "ready", "realm", "rebel", "refer", "reign", "relax", "relay", "remix",
    "renew", "reply", "resin", "retro", "rhyme", "ridge", "rifle", "right",
    "rigid", "rider", "rival", "river", "roast", "robin", "robot", "rocky",
    "rogue", "roost", "rough", "round", "route", "royal", "rugby", "ruler",
    "rumor", "rural", "saint", "salad", "salon", "salty", "satin", "sauce",
    "scale", "scarf", "scene", "scent", "scope", "score", "scout", "scrap",
    "sense", "serve", "seven", "shade", "shaft", "shake", "shall", "shame",
    "shape", "share", "shark", "sharp", "shear", "sheep", "sheet", "shelf",
    "shell", "shift", "shine", "shiny", "shirt", "shock", "shore", "short",
    "shout", "shown", "sight", "sigma", "silly", "since", "siren", "sixth",
    "skate", "skill", "skirt", "skull", "slate", "slave", "sleek", "sleep",
    "slice", "slide", "slime", "slope", "small", "smart", "smash", "smile",
    "smoke", "snack", "snake", "sneak", "solar", "solid", "solve", "sonic",
    "sorry", "sound", "south", "space", "spade", "spare", "spark", "spawn",
    "speak", "spear", "speed", "spell", "spend", "spice", "spike", "spine",
    "spire", "spite", "split", "spoke", "spoon", "sport",
    "spray", "spree", "squad", "stack", "staff", "stage", "stain", "stair",
    "stake", "stalk", "stall", "stamp", "stand", "stare", "stark", "start",
    "state", "steal", "steam", "steel", "steep", "steer", "stern", "stick",
    "still", "sting", "stock", "stole", "stone", "stood", "storm", "story",
    "stout", "stove", "strap", "straw", "stray", "strip", "stuck", "study",
    "stuff", "stump", "style", "sugar", "suite", "sunny", "super", "surge",
    "swamp", "swarm", "swear", "sweat", "sweep", "sweet", "swift", "swing",
    "sword", "syrup", "table", "tacit", "taken", "talon", "tango", "taste",
    "teach", "tempo", "tenor", "tense", "tenth", "theft", "their", "theme",
    "there", "these", "thick", "thief", "thing", "think", "third", "thorn",
    "those", "three", "throw", "thumb", "tiger", "tight", "timer", "titan",
    "title", "toast", "today", "token", "tonic", "tooth", "topaz", "topic",
    "torch", "total", "touch", "tough", "towel", "tower", "toxic", "trace",
    "track", "trade", "trail", "train", "trait", "tramp", "trash", "treat",
    "trend", "trial", "tribe", "trick", "tried", "troop", "trout", "truce",
    "truck", "truly", "trump", "trunk", "trust", "truth", "tulip", "tumor",
    "tunic", "turbo", "tutor", "twice", "twist", "ultra", "uncle", "under",
    "union", "unite", "unity", "upper", "upset", "urban", "usage", "usual",
    "valid", "value", "valve", "vapor", "vault", "venom", "venue", "verge",
    "verse", "video", "vigil", "villa", "vinyl", "viper", "viral", "virus",
    "visit", "vital", "vivid", "vocal", "vodka", "vogue", "voice", "vouch",
    "wagon", "waist", "waste", "watch", "water", "weary", "weave", "wedge",
    "weird", "whale", "wheat", "wheel", "where", "which", "while", "whirl",
    "whisk", "white", "whole", "widow", "width", "wield", "windy", "witch",
    "witty", "woman", "world", "worry", "worse", "worst", "worth", "would",
    "wound", "wrath", "wreck", "wrist", "write", "wrong", "yacht", "yield",
    "young", "youth", "zebra", "zesty",
}

# Tier -> (label, weight). Weight drives the site's ordering, so it also
# decides what a viewer sees first when several land at once.
TIERS = {
    "legendary": 100,
    "epic": 70,
    "rare": 45,
    "solid": 25,
    "plain": 10,
    "junk": 0,
}


def _pronounceable(name: str) -> bool:
    """True for names that read like a word rather than a licence plate.

    The test is that no three consecutive characters are all consonants and
    the name contains at least one vowel - which is what separates `mavlo`
    and `zurik` from `xkqzf`.
    """
    if not any(c in VOWELS for c in name):
        return False
    run = 0
    for c in name:
        if c in LETTERS and c not in VOWELS:
            run += 1
            if run >= 3:
                return False
        else:
            run = 0
    return True


def _patterned(name: str) -> bool:
    """Repeats and mirrors people actually chase: aaa, abab, aba, 1221."""
    if len(set(name)) == 1:
        return True
    if name == name[::-1]:
        return True
    if len(name) == 4 and name[:2] == name[2:]:
        return True
    return False


def _embedded_word(name: str) -> str | None:
    """The longest word from WORDS sitting inside *name*, if any."""
    for size in (5, 4, 3):
        for i in range(0, len(name) - size + 1):
            piece = name[i:i + size]
            if piece.isalpha() and piece in WORDS:
                return piece
    return None


# What earns a place on the board rather than merely being free.
#
# Free names are not scarce and almost none of them are worth having. Measured
# over 21,069 real five-character finds: not one was digit-free, so nothing
# reached the letters-only tiers, and not one was patterned either - `rate`
# alone would have kept everything or nothing. What separates the handful
# somebody would actually type is a real word surviving inside the name with
# at most one digit around it: mud5c, d6bug, box8j, 6cowv, 4vhit, 9fixq.
#
# That rule kept 29 of the 21,069 - about one in 726 - which is roughly a
# dozen names from a run that finds ten thousand. Sparse on purpose.
NOTEWORTHY_TIER = TIERS["solid"]
NOTEWORTHY_MAX_DIGITS = 1


def is_noteworthy(name: str) -> bool:
    """True for a name worth putting on the board.

    Anything at `solid` or above qualifies on its own - a palindrome, a
    repeat, or any name that manages to be letters-only. Below that, a name
    has to read: at most one digit, pronounceable, and carrying a word.
    """
    low = name.lower()
    if rate(low)[1] >= NOTEWORTHY_TIER:
        return True
    if sum(c.isdigit() for c in low) > NOTEWORTHY_MAX_DIGITS:
        return False
    return _pronounceable(low) and _embedded_word(low) is not None


def rate(name: str) -> tuple[str, int]:
    """Return (tier, weight) for *name*."""
    low = name.lower()
    letters_only = all(c in LETTERS for c in low)

    if low in WORDS or len(set(low)) == 1:
        return "legendary", TIERS["legendary"]
    if letters_only and _pronounceable(low):
        return "epic", TIERS["epic"]
    if letters_only:
        return "rare", TIERS["rare"]
    if _patterned(low):
        return "solid", TIERS["solid"]
    if _pronounceable(low):
        return "plain", TIERS["plain"]
    return "junk", TIERS["junk"]
