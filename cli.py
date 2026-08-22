"""Small local console client; it does not connect to VK."""

import logging

from bot_logic import BotLogic, ResponseType
from database import connect


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger(__name__).info("Starting FAQ CLI")
    connection = connect()
    try:
        try:
            vk_user_id = int(input("User ID: ").strip())
        except ValueError:
            print("User ID должен быть целым числом.")
            return
        bot = BotLogic(connection)
        print("Введите вопрос (или 'выход').")
        while True:
            text = input("> ")
            if text.strip().lower() in {"выход", "exit", "quit"}:
                break
            response = bot.handle_message(vk_user_id, text)
            if response.type is ResponseType.OPERATOR_MODE:
                print("Бот: [автоматический ответ отключён: диалог ведёт оператор]")
            else:
                print(f"Бот: {response.text}")
    except (KeyboardInterrupt, EOFError):
        print("\nВыход.")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
