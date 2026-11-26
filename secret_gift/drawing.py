from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Iterable


class DrawError(ValueError):
    """Raised when no complete assignment can be generated."""


@dataclass(frozen=True)
class DrawParticipant:
    id: str
    area: str = ""


def generate_draw(
    participants: Iterable[DrawParticipant],
    blocked_pairs: set[tuple[str, str]] | None = None,
    avoid_same_area: bool = False,
) -> dict[str, str]:
    """Return a complete giver -> recipient matching or raise DrawError.

    A randomized augmenting-path matcher finds a perfect bipartite matching.
    No partial result is returned to callers.
    """

    people = list(participants)
    if len(people) < 2:
        raise DrawError("Se necesitan al menos 2 participantes confirmados.")
    if len({person.id for person in people}) != len(people):
        raise DrawError("Hay identificadores de participantes duplicados.")

    blocked = blocked_pairs or set()
    rng = secrets.SystemRandom()
    giver_order = people[:]
    rng.shuffle(giver_order)

    allowed: dict[str, list[str]] = {}
    for giver in people:
        choices = []
        giver_area = giver.area.strip().casefold()
        for recipient in people:
            recipient_area = recipient.area.strip().casefold()
            same_area = bool(giver_area and recipient_area and giver_area == recipient_area)
            if recipient.id == giver.id:
                continue
            if (giver.id, recipient.id) in blocked:
                continue
            if avoid_same_area and same_area:
                continue
            choices.append(recipient.id)
        rng.shuffle(choices)
        allowed[giver.id] = choices

    recipient_to_giver: dict[str, str] = {}

    def assign(giver_id: str, seen: set[str]) -> bool:
        for recipient_id in allowed[giver_id]:
            if recipient_id in seen:
                continue
            seen.add(recipient_id)
            previous = recipient_to_giver.get(recipient_id)
            if previous is None or assign(previous, seen):
                recipient_to_giver[recipient_id] = giver_id
                return True
        return False

    for giver in giver_order:
        if not assign(giver.id, set()):
            raise DrawError(
                "No existe una combinación completa con las restricciones actuales."
            )

    result = {giver: recipient for recipient, giver in recipient_to_giver.items()}
    validate_draw(result, {person.id for person in people})
    return result


def validate_draw(assignments: dict[str, str], participant_ids: set[str]) -> None:
    if set(assignments) != participant_ids:
        raise DrawError("No todos los participantes tienen destinatario.")
    recipients = list(assignments.values())
    if set(recipients) != participant_ids or len(recipients) != len(set(recipients)):
        raise DrawError("Los destinatarios no forman una asignación única y completa.")
    if any(giver == recipient for giver, recipient in assignments.items()):
        raise DrawError("El sorteo contiene una autoasignación.")

