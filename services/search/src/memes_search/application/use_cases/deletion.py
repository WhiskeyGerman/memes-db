"""Атомарное удаление данных и permanent fence поздних private запросов."""

from uuid import UUID

from memes_search.application.ports.clock import IClock
from memes_search.application.ports.private_data import IPrivateDataRepository
from memes_search.application.ports.sample_admission import ISampleAdmission


class DeletePrivateData:
    """Сценарий стирания с одной транзакционной границей владельца."""

    def __init__(
        self, *, repository: IPrivateDataRepository, admission: ISampleAdmission, clock: IClock
    ) -> None:
        """Получить порт атомарного fencing и серверное время."""
        self._repository = repository
        self._admission = admission
        self._clock = clock

    async def execute(self, user_id: UUID) -> None:
        """Запретить поздние записи и надёжно отозвать неизвестные task IDs."""
        await self._repository.erase_owner(user_id, self._clock.now())
        await self._admission.erase_owner(user_id)
