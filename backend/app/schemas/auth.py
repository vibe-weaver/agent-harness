from pydantic import BaseModel, Field, field_validator
import re


class LoginRequest(BaseModel):
    email: str = Field(..., description="登录邮箱")
    password: str = Field(..., min_length=8, max_length=64, description="密码（8 位以上）")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        # 登录只做「查库比对」，不限制邮箱域名。
        # 初始管理员的邮箱由 INITIAL_ADMIN_EMAIL 决定，留空时是 admin@local
        # （见 main.py 首次启动逻辑）—— 在这里强制 QQ 邮箱，会让用默认配置
        # 启动的实例永远登录不了（README 里写的登录账号正是 admin@local）。
        # 注册仍要求真实 QQ 邮箱（要收验证码），见 RegisterRequest。
        v = v.strip().lower()
        if not v or "@" not in v:
            raise ValueError("请输入邮箱")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("密码至少 8 位")
        return v


class RegisterRequest(BaseModel):
    email: str = Field(..., description="QQ 邮箱")
    password: str = Field(..., min_length=8, max_length=64, description="密码（8 位以上）")
    code: str = Field(..., min_length=6, max_length=6, description="邮箱验证码")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[1-9]\d{4,10}@qq\.com$", v):
            raise ValueError("请输入正确的 QQ 邮箱")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("密码至少 8 位")
        return v


class SendCodeRequest(BaseModel):
    email: str = Field(..., description="QQ 邮箱")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[1-9]\d{4,10}@qq\.com$", v):
            raise ValueError("请输入正确的 QQ 邮箱")
        return v


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str | None = None


class UserInfo(BaseModel):
    id: int
    email: str | None = None
    username: str | None = None
    is_admin: bool = False
