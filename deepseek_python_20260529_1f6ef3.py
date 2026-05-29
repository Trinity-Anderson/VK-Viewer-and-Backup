import json
import os
import sys
import time
import threading
import tkinter as tk
from tkinter import messagebox, Listbox, MULTIPLE, END, scrolledtext
from datetime import datetime
import vk_api
import schedule
import pystray
from PIL import Image, ImageDraw

# -------------------- Конфигурация --------------------
CONFIG_FILE = "selected_chats.json"
TOKEN_FILE = "token.json"
BACKUP_DIR = "vk_backups"
BACKUP_TIME = "03:00"  # время ежедневного авто-бэкапа
LOG_FILE = "backup.log"

# -------------------- Глобальный логгер (пишет в файл и уведомляет GUI) --------------------
log_listeners = []  # список функций, которые надо вызвать при новом сообщении
log_lock = threading.Lock()

def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp}] {message}"
    with log_lock:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
        for listener in log_listeners:
            try:
                listener(line)
            except:
                pass

def add_log_listener(func):
    with log_lock:
        log_listeners.append(func)

def remove_log_listener(func):
    with log_lock:
        if func in log_listeners:
            log_listeners.remove(func)

# -------------------- Управление токеном --------------------
def load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            token = data.get("token")
            if token:
                return token
    return None

def save_token(token):
    with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
        json.dump({"token": token}, f, ensure_ascii=False, indent=2)

# -------------------- VK API --------------------
def get_vk(token):
    session = vk_api.VkApi(token=token)
    return session.get_api()

def fetch_chat_list(vk):
    """Возвращает список (peer_id, название) всех диалогов."""
    conversations = []
    offset = 0
    private_ids = []
    while True:
        try:
            resp = vk.messages.getConversations(count=200, offset=offset)
        except vk_api.exceptions.ApiError as e:
            log(f"Ошибка getConversations: {e}")
            break
        items = resp.get('items', [])
        if not items:
            break
        for item in items:
            conv = item['conversation']
            peer = conv['peer']
            peer_id = peer['id']
            title = "Личное сообщение"
            if 'chat_settings' in conv:
                title = conv['chat_settings'].get('title', 'Беседа без названия')
            else:
                private_ids.append(peer_id)
            conversations.append([peer_id, title, 'chat_settings' in conv])
        offset += 200
        if len(items) < 200:
            break
        time.sleep(0.4)

    # Разрешаем имена для личных диалогов
    if private_ids:
        names = resolve_names(vk, private_ids)
        for conv in conversations:
            pid, _, is_chat = conv
            if not is_chat and pid in names:
                conv[1] = names[pid]
    return [(pid, title) for pid, title, _ in conversations]

def resolve_names(vk, private_ids):
    user_ids = [pid for pid in private_ids if pid >= 0 and pid < 2000000000]
    group_ids = [abs(pid) for pid in private_ids if pid < 0]
    names = {}
    if user_ids:
        try:
            resp = vk.users.get(user_ids=','.join(map(str, user_ids)))
            for u in resp:
                names[u['id']] = f"{u['first_name']} {u['last_name']}"
        except Exception as e:
            log(f"Ошибка users.get: {e}")
    if group_ids:
        try:
            resp = vk.groups.getById(group_ids=','.join(map(str, group_ids)))
            for g in resp:
                names[-g['id']] = g['name']
        except Exception as e:
            log(f"Ошибка groups.getById: {e}")
    return names

def backup_chat(vk, peer_id):
    """Скачивает все сообщения из чата и сохраняет в JSON."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    all_messages = []
    offset = 0
    log(f"Старт выгрузки peer_id={peer_id}")
    while True:
        try:
            response = vk.messages.getHistory(
                peer_id=peer_id,
                offset=offset,
                count=200,
                rev=0
            )
        except vk_api.exceptions.ApiError as e:
            log(f"Ошибка getHistory для {peer_id}: {e}")
            break
        items = response['items']
        if not items:
            break
        all_messages.extend(items)
        offset += 200
        if len(items) < 200:
            break
        time.sleep(0.35)
    filename = os.path.join(BACKUP_DIR, f"chat_{peer_id}.json")
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(all_messages, f, ensure_ascii=False, indent=2)
    log(f"Сохранено {len(all_messages)} сообщений в {filename}")

def run_backup():
    """Выполняет бэкап для всех выбранных чатов."""
    token = load_token()
    if not token:
        log("Нет токена – бэкап невозможен")
        return
    if not os.path.exists(CONFIG_FILE):
        log("Нет списка чатов – бэкап невозможен")
        return
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        peers = json.load(f)
    vk = get_vk(token)
    log(f"Ежедневный бэкап {len(peers)} чатов...")
    for peer in peers:
        backup_chat(vk, peer)
    log("Бэкап завершён")

# -------------------- GUI окна (настройка, логи) --------------------
def run_setup_wizard():
    """
    Запускает полный мастер настройки: запрос токена (если нет) и выбор чатов.
    Может вызываться из любого потока, создаёт свой временный tk.Tk.
    """
    root = tk.Tk()
    root.withdraw()  # главное окно не показываем

    token = load_token()
    if not token:
        # Окно ввода токена
        win = tk.Toplevel(root)
        win.title("Введите токен VK")
        win.geometry("400x150")
        win.resizable(False, False)
        tk.Label(win, text="Access token из Kate Mobile:", font=("Arial", 10)).pack(pady=10)
        token_var = tk.StringVar()
        entry = tk.Entry(win, textvariable=token_var, width=50, show="*")
        entry.pack(pady=5)
        entry.focus()

        def on_ok():
            t = token_var.get().strip()
            if t:
                save_token(t)
                log("Токен сохранён")
                win.destroy()
            else:
                messagebox.showerror("Ошибка", "Токен не может быть пустым", parent=win)

        tk.Button(win, text="Сохранить", command=on_ok).pack(pady=10)
        win.grab_set()
        root.wait_window(win)
        token = load_token()
        if not token:
            root.destroy()
            return

    # Теперь выбор чатов
    vk = get_vk(token)
    try:
        chats = fetch_chat_list(vk)
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось получить список чатов:\n{e}")
        root.destroy()
        return

    if not chats:
        messagebox.showerror("Ошибка", "Список чатов пуст")
        root.destroy()
        return

    win2 = tk.Toplevel(root)
    win2.title("Выберите чаты для бэкапа")
    win2.geometry("550x500")
    tk.Label(win2, text="Выберите чаты (можно несколько):", font=("Arial", 10)).pack(pady=5)

    frame = tk.Frame(win2)
    frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
    scrollbar = tk.Scrollbar(frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    listbox = Listbox(frame, selectmode=MULTIPLE, yscrollcommand=scrollbar.set, font=("Consolas", 9))
    for peer_id, title in chats:
        listbox.insert(END, f"{title}  (peer_id={peer_id})")
    listbox.pack(fill=tk.BOTH, expand=True)
    scrollbar.config(command=listbox.yview)

    def on_save():
        selected_indices = listbox.curselection()
        if not selected_indices:
            messagebox.showwarning("Предупреждение", "Ничего не выбрано", parent=win2)
            return
        selected_peers = [chats[i][0] for i in selected_indices]
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(selected_peers, f, ensure_ascii=False, indent=2)
        log(f"Выбрано {len(selected_peers)} чатов: {selected_peers}")
        messagebox.showinfo("Готово", f"Выбрано {len(selected_peers)} чатов", parent=win2)
        win2.destroy()

    tk.Button(win2, text="Сохранить выбранные", command=on_save).pack(pady=10)
    win2.grab_set()
    root.wait_window(win2)
    root.destroy()

# -------------------- Окно логов --------------------
class LogWindow:
    def __init__(self):
        self.win = None
        self.text_widget = None
        self.listener_func = None

    def show(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.lift()
            return
        self.win = tk.Toplevel()
        self.win.title("Логи VK Backup")
        self.win.geometry("700x400")
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)

        self.text_widget = scrolledtext.ScrolledText(self.win, wrap=tk.WORD, state='disabled', font=("Consolas", 9))
        self.text_widget.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Загружаем существующий лог-файл
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
            self.text_widget.config(state='normal')
            self.text_widget.insert(tk.END, content)
            self.text_widget.see(tk.END)
            self.text_widget.config(state='disabled')

        # Регистрируем слушатель
        self.listener_func = lambda line: self.append_log(line)
        add_log_listener(self.listener_func)

    def append_log(self, line):
        if self.win is None or not self.win.winfo_exists():
            return
        self.text_widget.config(state='normal')
        self.text_widget.insert(tk.END, line + "\n")
        self.text_widget.see(tk.END)
        self.text_widget.config(state='disabled')

    def on_close(self):
        remove_log_listener(self.listener_func)
        self.win.destroy()
        self.win = None

# -------------------- Иконка в трее --------------------
def create_tray_image():
    img = Image.new('RGB', (64, 64), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=(0, 120, 212))
    draw.text((22, 18), "V", fill=(255, 255, 255), font_size=30)
    return img

class TrayApp:
    def __init__(self):
        self.icon = None
        self.scheduler_thread = None
        self.running = True
        self.log_window = None

    def run_scheduler(self):
        schedule.every().day.at(BACKUP_TIME).do(run_backup)
        log(f"Планировщик запущен, бэкап ежедневно в {BACKUP_TIME}")
        while self.running:
            schedule.run_pending()
            time.sleep(30)

    def start(self):
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        self.scheduler_thread.start()

        menu = pystray.Menu(
            pystray.MenuItem("Сделать бэкап сейчас", self.on_backup_now),
            pystray.MenuItem("Сменить токен", self.on_change_token),
            pystray.MenuItem("Выбрать чаты заново", self.on_change_chats),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть консоль (логи)", self.on_show_log),
            pystray.MenuItem("Выход", self.on_exit)
        )

        self.icon = pystray.Icon("VK Backup", create_tray_image(), "VK Backup", menu)
        log("Иконка в трее запущена")
        self.icon.run()

    def on_backup_now(self, icon, item):
        log("Ручной запуск бэкапа")
        threading.Thread(target=run_backup, daemon=True).start()

    def on_change_token(self, icon, item):
        def _gui():
            root = tk.Tk()
            root.withdraw()
            win = tk.Toplevel(root)
            win.title("Новый токен")
            win.geometry("400x130")
            tk.Label(win, text="Введите новый токен:").pack(pady=10)
            token_var = tk.StringVar()
            entry = tk.Entry(win, textvariable=token_var, width=50, show="*")
            entry.pack()
            entry.focus()
            def save():
                t = token_var.get().strip()
                if t:
                    save_token(t)
                    log("Токен обновлён")
                    messagebox.showinfo("Успех", "Токен сохранён", parent=win)
                    win.destroy()
                    root.destroy()
            tk.Button(win, text="Сохранить", command=save).pack(pady=10)
            win.grab_set()
            root.mainloop()
        threading.Thread(target=_gui, daemon=True).start()

    def on_change_chats(self, icon, item):
        log("Открытие мастера выбора чатов")
        threading.Thread(target=run_setup_wizard, daemon=True).start()

    def on_show_log(self, icon, item):
        # Запускаем окно логов в основном потоке GUI (используем pystray'евский вызов через icon.visible или отдельный поток)
        # Создадим окно в отдельном потоке с новым tk.Tk
        def _open_log():
            root = tk.Tk()
            root.withdraw()
            log_win = LogWindow()
            log_win.show()
            root.mainloop()
        threading.Thread(target=_open_log, daemon=True).start()

    def on_exit(self, icon, item):
        log("Выход из программы")
        self.running = False
        if self.icon:
            self.icon.stop()
        sys.exit(0)

# -------------------- Точка входа --------------------
if __name__ == "__main__":
    # Если нет настроек – запускаем мастер
    if not os.path.exists(TOKEN_FILE) or not os.path.exists(CONFIG_FILE):
        log("Первый запуск – мастер настройки")
        run_setup_wizard()
    else:
        log("Настройки найдены, запуск в трее")

    app = TrayApp()
    app.start()