from pydantic import BaseModel


class CornnorConfig(BaseModel):
    botToken: str
    triggerWords: list[str]
    adminIds: list[int]
    modelName: str = "gemini-3.6-flash"
    apiEndpoint: str
    apiKey: str
    maxHistory: int
    maxImageCache: int
    maxImageSizeByte: int
    extensionsDir: str = "extensions"