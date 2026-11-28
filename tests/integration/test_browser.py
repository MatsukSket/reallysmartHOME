"""Запуск с --headed через SHOW_BROWSER=1, с реальной сценой через SCENE_EXE."""
import os
from pathlib import Path
import pytest
from test_demo import house

def test_browser(house):
    sync=pytest.importorskip("playwright.sync_api")
    c,sim,_,_=house
    with sync.sync_playwright() as p:
        browser=p.chromium.launch(channel=os.getenv("BROWSER_CHANNEL","msedge"),
                    headless=os.getenv("SHOW_BROWSER")!="1",args=["--window-position=0,40","--window-size=720,850"])
        page=browser.new_page(viewport={"width":680,"height":720})
        page.goto(str(c.base_url))
        page.locator('#login [name=username]').fill("admin")
        page.locator('#login [name=password]').fill("demo-test")
        page.get_by_role("button",name="Войти",exact=True).click()
        page.locator("#home-rooms button").filter(has_text="Кухня").click()
        page.locator("#dev-list button").filter(has_text="Котёл").click()
        boiler = page.locator("#m-dev")
        sync.expect(boiler.locator("#d-ctl")).to_contain_text("Текущая температура", timeout=10000)
        sync.expect(boiler.locator("#d-settings input")).to_have_count(1)
        if os.getenv("CHECK_GODOT_TELEMETRY"):
            sync.expect(boiler.locator("#d-ctl")).to_contain_text("Текущая температура, °C · 47.5")
            sync.expect(boiler.locator("#d-ctl")).to_contain_text("Давление, бар · 2.1")
        boiler.get_by_label("Целевая температура воды, °C").fill("64")
        boiler.get_by_role("button", name="Сохранить", exact=True).click()
        sync.expect(boiler.locator("#d-ctl")).to_contain_text("Целевая температура воды, °C · 64")
        boiler.locator("[data-close]").click()
        page.locator("[data-tab=s-home]").click()
        page.locator("#home-rooms button").filter(has_text="Гостиная").click()
        page.locator("#dev-list button").filter(has_text="Кондиционер").click()
        card=page.locator("#m-dev")
        sync.expect(card).to_be_visible()
        if os.getenv("CHECK_GODOT_TELEMETRY"):
            sync.expect(card.locator("#d-ctl")).to_contain_text("Текущая температура воздуха, °C · 18.5")
        sync.expect(card).not_to_contain_text("Ожидание показаний",timeout=20000)
        temperature = card.get_by_label("Целевая температура воздуха, °C")
        temperature.fill("21")
        temperature.locator("xpath=ancestor::form").get_by_role("button", name="Сохранить").click()
        sync.expect(card.locator("#d-ctl")).to_contain_text("Целевая температура воздуха, °C · 21")
        mode = card.get_by_label("Режим кондиционера")
        mode.select_option("cool")
        mode.locator("xpath=ancestor::form").get_by_role("button", name="Сохранить").click()
        sync.expect(card.locator("#d-ctl")).to_contain_text("Режим · cool")
        if os.getenv("CHECK_GODOT_TELEMETRY"):
            snapshot = page.evaluate("api('home')")
            devices = {d["id"]: d["state"] for room in snapshot["rooms"] for d in room["devices"]}
            assert devices["sensor_temperature_living"]["value"] == 32.5
            assert devices["sensor_leak_bathroom"]["battery"] == 63
            assert devices["leak_protection"]["jammed"] is True
        card.get_by_role("button",name="Включить",exact=True).click()
        sync.expect(card.locator("#d-feedback")).to_contain_text("выполнено")
        sync.expect(card).to_contain_text("Питание · да")
        card.locator("[data-close]").click()
        if sim.stdin:
            sim.stdin.write(b"e\n");sim.stdin.flush()
        page.locator("[data-tab=s-log]").click()
        sync.expect(page.locator("#audit")).to_contain_text("E04",timeout=10000)
        page.get_by_role("button",name="Помощник",exact=True).click()
        message = page.get_by_role("textbox", name="Сообщение помощнику")
        sync.expect(message).to_have_value("")
        assert message.get_attribute("placeholder") is None
        sync.expect(message).to_have_attribute("autocomplete", "off")
        message.fill("Что означает E04?")
        page.get_by_role("button",name="Отправить").click()
        sync.expect(page.locator("#answer")).not_to_have_text("Ищу ответ…",timeout=180000)
        sync.expect(page.locator("#sources")).to_contain_text("boiler_baxi",timeout=5000)
        answer=page.locator("#answer").inner_text()
        assert "давлен" in answer.lower(),answer
        print("Ответ помощника:",answer)
        sync.expect(page.locator("#confirm")).to_be_hidden()
        assert "conditioner_sim" not in page.locator("#sources").inner_text()
        home = page.evaluate("api('home')")
        ac = next(d for room in home["rooms"] for d in room["devices"] if d["id"] == "conditioner_living")
        assert ac["state"]["mode"] == "cool"

        page.locator("[data-tab=s-home]").click()
        page.locator("#home-rooms button").filter(has_text="Кухня").click()
        page.locator("#dev-list button").filter(has_text="Котёл").click()
        sync.expect(card.locator("#d-warning")).to_contain_text("E04")
        sync.expect(card.get_by_role("button", name="Включить", exact=True)).to_be_disabled()
        sync.expect(card.get_by_role("button", name="Сбросить ошибку")).to_have_count(0)
        if sim.stdin:
            sim.stdin.write(b"r\n")
            sim.stdin.flush()
            sync.expect(card.locator("#d-ctl")).to_contain_text("1.4")
            sync.expect(card.locator("#d-warning")).to_contain_text("E04")
            sim.stdin.write(b"c\nl\n")
            sim.stdin.flush()
            sync.expect(card.locator("#d-warning")).to_be_hidden()
            card.get_by_role("button", name="Включить", exact=True).click()
            sync.expect(card).to_contain_text("Питание · да")
            card.locator("[data-close]").click()
            page.locator("[data-tab=s-home]").click()
            page.locator("#home-rooms button").filter(has_text="Ванная").click()
            page.locator("#dev-list button").filter(has_text="Защита от протечек").click()
            sync.expect(card.locator("#d-warning")).to_contain_text("Датчик фиксирует протечку")
            sync.expect(card.get_by_role("button", name="Открыть", exact=True)).to_be_disabled()
            sim.stdin.write(b"d\n")
            sim.stdin.flush()
            card.get_by_role("button", name="Открыть", exact=True).click()
            sync.expect(card.locator("#d-feedback")).to_contain_text("выполнено")
        card.locator("[data-close]").click()
        live_calls = []
        page.on("request", lambda request: live_calls.append(request.url) if request.url.endswith("/api/live") else None)
        backend = house[2]
        backend.terminate()
        backend.wait(timeout=10)
        sync.expect(page.locator("#link")).to_contain_text("Восстанавливаем связь")
        house[3]()
        sync.expect(page.locator("#link")).to_contain_text("поток подключён",timeout=20000)
        page.locator("[data-tab=s-home]").click()
        page.locator("#home-rooms button").filter(has_text="Гостиная").click()
        page.locator("#dev-list button").filter(has_text="Кондиционер").click()
        sync.expect(card).to_contain_text("Питание · да")
        assert live_calls

        card.locator("[data-close]").click()
        page.locator("[data-tab=s-dev]").click()
        page.locator('[data-modal="m-room"]').click()
        page.locator("#room-name").fill("Кабинет")
        page.locator("#create-room button").click()
        page.locator("#plan button").filter(has_text="Кабинет").click()
        sync.expect(page.locator("#dev-list")).to_be_empty()
        page.locator("#room-add").click()
        if os.getenv("CHECK_GODOT_CATALOG"):
            sync.expect(page.locator("#add-device")).to_contain_text("Лампа для чтения", timeout=10000)
            page.locator("#add-device").select_option(label="Лампа для чтения")
        else:
            page.locator("#add-device").select_option("light_spare")
        room_picker = page.locator("#add-room")
        sync.expect(room_picker).to_have_css("background-color", "rgb(255, 255, 255)")
        if page.evaluate("CSS.supports('appearance', 'base-select')"):
            room_picker.click()
            sync.expect(page.locator("#add-room:open")).to_be_visible()
            page.keyboard.press("Escape")
            sync.expect(page.locator("#add-room:open")).to_have_count(0)
        page.locator("#add-submit").click()
        lamp_name = "Лампа для чтения" if os.getenv("CHECK_GODOT_CATALOG") else "Запасная лампа"
        page.locator("#dev-list button").filter(has_text=lamp_name).click()
        if os.getenv("CHECK_GODOT_CATALOG"):
            sync.expect(card.locator("#d-ctl")).to_contain_text("Яркость, % · 46")
        if os.getenv("CHECK_GODOT_TELEMETRY"):
            sync.expect(card.locator("#d-ctl")).to_contain_text("Питание от сети · нет")
            sync.expect(card.locator("#d-warning")).to_contain_text("E31")
            sync.expect(card.get_by_role("button", name="Включить", exact=True)).to_be_disabled()
        else:
            card.get_by_label("Яркость, %").fill("77")
            card.get_by_role("button", name="Сохранить", exact=True).click()
            sync.expect(card.locator("#d-ctl")).to_contain_text("Яркость, % · 77")
            sync.expect(card.locator("#d-feedback")).to_contain_text("выполнено")
        page.on("dialog", lambda dialog: dialog.accept())
        if os.getenv("CHECK_GODOT_REMOVAL"):
            sync.expect(card).to_be_hidden(timeout=10000)
        else:
            card.get_by_role("button", name="Отключить от дома").click()
        sync.expect(page.locator("#dev-list")).to_be_empty()
        page.locator("#room-remove").click()
        sync.expect(page.locator("#plan")).not_to_contain_text("Кабинет")
        page.locator('#s-dev [data-modal="m-add"]').click()
        if os.getenv("CHECK_GODOT_REMOVAL"):
            sync.expect(page.locator("#add-device")).to_contain_text("Запасная лампа")
            sync.expect(page.locator("#add-device")).not_to_contain_text(lamp_name)
        else:
            sync.expect(page.locator("#add-device")).to_contain_text(lamp_name)
        page.locator("#m-add [data-close]").click()

        page.locator("[data-tab=s-chat]").click()
        page.locator("#chat input").fill("Выставь кондиционеру 23 градуса")
        page.locator("#chat button").click()
        sync.expect(page.locator("#answer")).to_contain_text("выполнено")

        page.clock.install()
        notification = page.locator("#status")
        page.evaluate("result({status: 'отклонено', reason: 'Ошибка E04'})")
        sync.expect(notification).to_contain_text("Ошибка E04")
        page.clock.fast_forward(5000)
        page.evaluate("msg('Новое уведомление')")
        page.clock.fast_forward(2500)
        sync.expect(notification).to_have_text("Новое уведомление")
        page.clock.fast_forward(4500)
        sync.expect(notification).to_be_empty()
        browser.close()
