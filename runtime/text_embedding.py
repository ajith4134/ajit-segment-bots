"""The one place a text becomes a vector on this box.

`knowledge-embedder` holds the chunking, the caching and the model stamp, and
asks for one thing: a callable that turns one piece of text into a vector. This
module is that callable, and nothing else imports a model.

Kept out of the part for the reason every runtime module here is: a part states
what it needs and the substrate answers how (RL-069). It is also what makes the
degraded state honest -- when a model cannot be loaded this raises, the part
installs nothing, and every text is answered `no-embedding-model-is-installed` by
name rather than with a zero vector that would sit in the index looking like a
measurement.

**One thread, deliberately.** 332 parts share twelve cores, and torch defaults to
every core it can see. Measured 2026-09-16 on this box at one thread:
25.1 ms for a single text, 6.1 ms each in a batch of 32 -- 165 texts a second,
which is far more than the documents, skills and trade narratives this system
produces. Taking the whole machine to make that faster would starve the parts
that trade.
"""

from __future__ import annotations

import threading

# Loaded once, on first use, and shared. Held at module level rather than passed
# because the model is 90 MB of weights and a second copy buys nothing.
_LOCK = threading.Lock()
_LOADED: dict[str, object] = {}


def load_sentence_embedder(model_id: str, dimensions: int, threads: int = 1):
    """A callable turning one text into a `dimensions`-long vector.

    Raises rather than returning a stub if the model cannot be loaded: the caller
    installs nothing and says so, which is the state this system can act on.
    """
    with _LOCK:
        existing = _LOADED.get(model_id)
        if existing is None:
            import torch
            from transformers import AutoModel, AutoTokenizer

            torch.set_num_threads(max(1, int(threads)))
            tokenizer = AutoTokenizer.from_pretrained(model_id)
            model = AutoModel.from_pretrained(model_id)
            model.eval()
            existing = (tokenizer, model, torch)
            _LOADED[model_id] = existing

    tokenizer, model, torch = existing

    def embed_one(text: str) -> tuple[float, ...]:
        """Mean-pooled, length-normalised, which is how this family is trained.

        Mean over the attention mask rather than over every position: padding
        tokens carry a vector too, and averaging them in moves a short text
        towards whatever the padding happens to mean. Normalised because the
        index compares by cosine, and an unnormalised vector makes a long
        document win on magnitude alone.
        """
        with torch.no_grad():
            batch = tokenizer(
                [str(text)], padding=True, truncation=True, max_length=256,
                return_tensors="pt",
            )
            hidden = model(**batch).last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1).float()
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            vector = torch.nn.functional.normalize(pooled, dim=1)[0]
        values = tuple(float(value) for value in vector)
        if len(values) != int(dimensions):
            raise ValueError(
                f"{model_id} returns {len(values)} dimensions where {dimensions} was "
                f"declared; a vector of the wrong length in a shared index is a silent "
                f"corruption"
            )
        return values

    return embed_one
