from zomboid_host.settings import load_settings, render_ini


def test_load_settings_defaults(tmp_path):
    p = tmp_path / "s.toml"; p.write_text('server_name = "pzfriends"\n')
    s = load_settings(p)
    assert s.server_name == "pzfriends" and s.idle_minutes == 30
    assert s.hard_limit_minutes == 480 and s.warn_minutes == (15, 5) and s.backup_retain == 14


def test_render_ini_replaces_and_appends():
    out = render_ini("Public=true\nPassword=\nMods=a;b\n",
                     {"Password": "pw", "SaveWorldEveryMinutes": "10"})
    assert out.splitlines() == ["Public=true", "Password=pw", "Mods=a;b", "SaveWorldEveryMinutes=10"]


def test_render_ini_keeps_values_with_equals_signs():
    assert render_ini("Motd=a=b\n", {}) == "Motd=a=b\n"
