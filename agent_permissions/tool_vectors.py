"""A local latent semantic index over the active catalog's descriptions and examples."""

from collections import Counter
from collections.abc import Collection, Mapping

import numpy as np


class ToolVectors:
    """Fit TF-IDF and truncated SVD once, then compare only the caller's allowed rows."""

    def __init__(self, documents: Mapping[str, Counter[str]]) -> None:
        self.rows = {operation: i for i, operation in enumerate(documents)}
        frequencies = Counter(term for terms in documents.values() for term in terms)
        vocabulary = sorted(frequencies)
        self.columns = {term: i for i, term in enumerate(vocabulary)}
        # Generic subjects shared by many operations are insufficient evidence
        # to offer a write when the query contains no recognizable action.
        specific = {
            term for term, n in frequencies.items() if n <= max(1, len(documents) // 6)
        }
        self.specific_terms = {
            op: terms.keys() & specific for op, terms in documents.items()
        }
        if not vocabulary:
            return

        counts = np.zeros((len(documents), len(vocabulary)))
        for row, terms in enumerate(documents.values()):
            for term, count in terms.items():
                counts[row, self.columns[term]] = count
        self.idf = 1 + np.log((1 + len(documents)) / (1 + (counts > 0).sum(axis=0)))
        weighted = np.log1p(counts) * self.idf
        weighted /= np.maximum(np.linalg.norm(weighted, axis=1, keepdims=True), 1e-12)
        # Related descriptions share latent dimensions. With a small catalog an
        # exact factorization is inexpensive and needs no training service or weights.
        _, _, components = np.linalg.svd(weighted, full_matrices=False)
        dimensions = min(64, max(1, (3 * len(documents) + 3) // 4))
        self.components = components[:dimensions]
        projected = weighted @ self.components.T
        self.vectors = projected / np.maximum(
            np.linalg.norm(projected, axis=1, keepdims=True), 1e-12
        )

    def similarities(
        self, terms: set[str], allowed: Collection[str]
    ) -> dict[str, float]:
        columns = [self.columns[t] for t in sorted(terms) if t in self.columns]
        operations = [op for op in allowed if op in self.rows]
        if not columns or not operations:
            return {}
        weights = self.idf[columns]
        query = self.components[:, columns] @ weights
        scores = self.vectors[[self.rows[op] for op in operations]] @ (
            query / max(np.linalg.norm(query), 1e-12)
        )
        return dict(zip(operations, scores.tolist(), strict=True))
