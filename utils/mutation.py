"""
Lightweight text mutation to simulate natural reply variation.
No AI APIs — deterministic randomization only.
"""

from __future__ import annotations

import random
import string


class TextMutator:
    """
    Applies safe cosmetic mutations to a reply string:
    - random trailing punctuation
    - optional emoji swap
    - capitalization tweaks
    """

    def __init__(
        self,
        punctuation_pool: list[str] | None = None,
        emoji_pool: list[str] | None = None,
        cap_probability: float = 0.25,
    ) -> None:
        self.punctuation = punctuation_pool or ["", ".", "!", "!!", "...", " 🙌", " 💯"]
        self.emoji_pool = emoji_pool or ["🔥", "💯", "🙌", "👏", "✨", "🚀", "💪"]
        self.cap_probability = cap_probability

    def mutate(self, text: str) -> str:
        if not text:
            return text

        # 1. Capitalization variation: randomly title-case or upper-first
        if random.random() < self.cap_probability:
            words = text.split()
            if words:
                idx = random.randrange(len(words))
                words[idx] = words[idx].capitalize()
                text = " ".join(words)
        elif random.random() < self.cap_probability:
            text = text[0].upper() + text[1:]

        # 2. Punctuation / emoji tail
        tail = random.choice(self.punctuation)
        if tail.startswith(" "):
            text = text.rstrip(string.punctuation) + tail
        else:
            text = text.rstrip(string.punctuation) + tail

        return text.strip()
