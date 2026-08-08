"""WTForms definitions.

Money fields accept what people actually type -- ``$1,234.56``, ``1234.56``,
``(1,234.56)`` -- and convert to integer cents through the money kernel, so a
float never enters the system by way of a form.
"""

from __future__ import annotations

from typing import Any

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    Field,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, Length, Optional, ValidationError
from wtforms.widgets import TextInput

from massingbill.models import Role
from massingbill.services.money import Cents, MoneyError, cents, parse_bp, parse_money, to_display


class MoneyField(Field):
    """A monetary amount, stored as integer cents.

    Rejects sub-cent precision rather than rounding it away: an amount someone
    typed into a pay application is not the place for a silent adjustment.

    Requiredness is handled here rather than with ``DataRequired``. A failed
    parse leaves ``data`` as ``None``, which ``DataRequired`` then reports as
    "This field is required" -- **clearing the real message in the process**, so
    somebody who typed ``1000.005`` is told their entry is missing. Owning the
    check keeps the message that actually explains the problem.
    """

    widget = TextInput()

    def __init__(
        self, label: str = "", validators: Any = None, *, required: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(label, validators, **kwargs)
        self.required = required
        self.data: Cents | None = None

    def _value(self) -> str:
        if self.raw_data:
            return self.raw_data[0]
        if self.data is None:
            return ""
        return to_display(self.data, symbol="")

    def process_formdata(self, valuelist: list[str]) -> None:
        raw = valuelist[0].strip() if valuelist else ""
        if not raw:
            self.data = None
            if self.required:
                raise ValueError("Enter an amount.")
            return
        try:
            self.data = parse_money(raw)
        except MoneyError as exc:
            self.data = None
            raise ValueError(str(exc)) from exc


class BasisPointField(Field):
    """A percentage, stored as basis points. ``10`` or ``10%`` both mean 1000."""

    widget = TextInput()

    def __init__(
        self, label: str = "", validators: Any = None, *, required: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(label, validators, **kwargs)
        self.required = required
        self.data: int | None = None

    def _value(self) -> str:
        if self.raw_data:
            return self.raw_data[0]
        if self.data is None:
            return ""
        return f"{self.data / 100:g}"

    def process_formdata(self, valuelist: list[str]) -> None:
        raw = valuelist[0].strip() if valuelist else ""
        if not raw:
            self.data = None
            if self.required:
                raise ValueError("Enter a percentage.")
            return
        try:
            self.data = parse_bp(raw)
        except MoneyError as exc:
            self.data = None
            raise ValueError(str(exc)) from exc


# ── Authentication ──────────────────────────────────────────────────────────


class SignInForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=254)])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Sign in")


class MfaForm(FlaskForm):
    code = StringField("Authentication code", validators=[DataRequired(), Length(min=6, max=8)])
    submit = SubmitField("Verify")


class RegisterForm(FlaskForm):
    name = StringField("Your name", validators=[Optional(), Length(max=200)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=254)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=12, max=255)])
    organization_name = StringField(
        "Company name", validators=[DataRequired(), Length(min=2, max=200)]
    )
    submit = SubmitField("Create account")


class EnrolMfaForm(FlaskForm):
    code = StringField("Code from your app", validators=[DataRequired(), Length(min=6, max=8)])
    submit = SubmitField("Enable two-factor")


# ── Organization ────────────────────────────────────────────────────────────


def _role_choices(include_external: bool = True) -> list[tuple[str, str]]:
    roles = list(Role)
    if not include_external:
        roles = [role for role in roles if role.is_internal]
    return [(str(role), role.label) for role in roles]


class InviteMemberForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=254)])
    role = SelectField("Role", choices=_role_choices(), validators=[DataRequired()])
    submit = SubmitField("Add member")


class ChangeRoleForm(FlaskForm):
    role = SelectField("Role", choices=_role_choices(), validators=[DataRequired()])
    submit = SubmitField("Save")


# ── Projects and contracts ──────────────────────────────────────────────────

US_STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
]


class ProjectForm(FlaskForm):
    number = StringField("Project number", validators=[DataRequired(), Length(max=64)])
    name = StringField("Project name", validators=[DataRequired(), Length(max=200)])
    address = TextAreaField("Address", validators=[Optional(), Length(max=2000)])
    jurisdiction_state = SelectField(
        "State",
        choices=[("", "Select a state")] + [(s, s) for s in US_STATES],
        validators=[DataRequired()],
        description="Selects the statutory retainage cap and lien-waiver forms that apply.",
    )
    is_public_work = BooleanField("Public work")
    is_residential = BooleanField("Residential")
    stories = IntegerField("Storeys", validators=[Optional()])
    submit = SubmitField("Save project")


class ContractForm(FlaskForm):
    number = StringField("Contract number", validators=[Optional(), Length(max=64)])
    original_contract_sum = MoneyField("Original contract sum", required=True)
    retainage_rate_work = BasisPointField("Retainage on completed work (%)", required=True)
    retainage_rate_stored = BasisPointField("Retainage on stored material (%)", required=True)
    stored_materials_allowed = BooleanField("Stored materials may be billed", default=True)
    offsite_stored_allowed = BooleanField("Off-site storage permitted")
    submit = SubmitField("Save contract")

    def validate_original_contract_sum(self, field: MoneyField) -> None:
        if field.data is not None and field.data <= 0:
            raise ValidationError("The contract sum must be greater than zero.")


# ── Schedule of values ──────────────────────────────────────────────────────


class SovLineForm(FlaskForm):
    item_no = StringField("Item no.", validators=[DataRequired(), Length(max=32)])
    description = TextAreaField("Description of work", validators=[DataRequired()])
    csi_code = StringField("CSI code", validators=[Optional(), Length(max=32)])
    group = StringField("Group", validators=[Optional(), Length(max=120)])
    scheduled_value = MoneyField("Scheduled value", required=True)
    retainage_rate = BasisPointField(
        "Line retainage (%)",
        description="Only used when the contract withholds variable retainage per line.",
    )
    is_general_conditions = BooleanField("General conditions")
    is_allowance = BooleanField("Allowance")
    submit = SubmitField("Save line")


class ConfirmForm(FlaskForm):
    """A bare CSRF-protected form for destructive or state-changing buttons."""

    submit = SubmitField("Confirm")


__all__ = [
    "US_STATES",
    "BasisPointField",
    "ChangeRoleForm",
    "ConfirmForm",
    "ContractForm",
    "EnrolMfaForm",
    "InviteMemberForm",
    "MfaForm",
    "MoneyField",
    "ProjectForm",
    "RegisterForm",
    "SignInForm",
    "SovLineForm",
    "cents",
]
