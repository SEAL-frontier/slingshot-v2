from cornnor import Cornnor, CornnorConfig

import json, logging

config_file = "config.json"
connor_config = CornnorConfig.model_validate_json(open(config_file, "r").read())

Cornnor(connor_config).run()
