from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    proxy = State()
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()


class RenameAccount(StatesGroup):
    title = State()


class AccProxy(StatesGroup):
    value = State()


class MailingFlow(StatesGroup):
    """Создание и правка рассылки: ввод текстом."""
    name = State()
    interval = State()  # КД между циклами
    chat_delay = State()  # КД между чатами
    message = State()  # новое сообщение-вариант
    target = State()
    folder = State()
    txt = State()
    hours = State()


class PromoFSM(StatesGroup):
    code = State()


class AddPlanFSM(StatesGroup):
    name = State()
    price = State()
    days = State()
    tier = State()


class AddPromoFSM(StatesGroup):
    code = State()
    type = State()
    value = State()
    tier = State()
    limit = State()


class MethodFSM(StatesGroup):
    value = State()


class Admin(StatesGroup):
    search = State()
    bc_msg = State()
    bc_btn = State()
