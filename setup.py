from plugin import F, create_plugin_instance  # type: ignore # pylint: disable=import-error

__menu = {
    "uri": __package__,
    "name": "SOOP KBO",
    "list": [
        {"uri": "setting", "name": "설정"},
        {"uri": "log", "name": "로그"},
    ],
}

setting = {
    "filepath": __file__,
    "use_db": True,
    "use_default_setting": True,
    "home_module": "setting",
    "menu": __menu,
    "setting_menu": None,
    "default_route": "single",
}

P = create_plugin_instance(setting)

from .logic import Logic  # noqa: E402

P.set_module_list([Logic])
