from bot.handlers import accounts, admin, admin_billing, billing, mailings, start

# start — первым: /start и /menu должны работать из любого шага ввода, а не записываться как текст
# сообщения/номера/названия. Дальше админские роутеры, потом остальные FSM-хендлеры.
routers = [start.router, admin.router, admin_billing.router, mailings.router, accounts.router, billing.router]
