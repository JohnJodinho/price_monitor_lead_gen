from pydantic import BaseModel, Field

class ContactsSchema(BaseModel):
    phone: list[str] = Field(default_factory=list)
    email: list[str] = Field(default_factory=list)
    emailWithDomain: list[str] = Field(default_factory=list)

class SocialsSchema(BaseModel):
    X_twitter: list[str] = Field(default_factory=list, alias="X(twitter)")
    Facebook: list[str] = Field(default_factory=list)
    Whatsapp: list[str] = Field(default_factory=list)
    Instagram: list[str] = Field(default_factory=list)
    linkedIn: list[str] = Field(default_factory=list)
