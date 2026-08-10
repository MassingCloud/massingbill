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
    DateField,
    Field,
    IntegerField,
    PasswordField,
    SelectField,
    SelectMultipleField,
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


# ── Applications for payment ────────────────────────────────────────────────


class OpenPeriodForm(FlaskForm):
    """Start the next requisition."""

    period_start = DateField("Period from", validators=[DataRequired()])
    period_end = DateField("Period to", validators=[DataRequired()])
    application_date = DateField("Application date", validators=[Optional()])
    submit = SubmitField("Open the period")

    def validate_period_end(self, field: Field) -> None:
        if self.period_start.data and field.data and field.data < self.period_start.data:
            raise ValidationError("The period cannot end before it starts.")


class PeriodLineForm(FlaskForm):
    """One G703 row, as typed.

    Not a ``FieldList``: the number of rows is whatever the schedule of values
    says, and each row needs its own error slot. The view instantiates one per
    line with a distinct field prefix.
    """

    class Meta:
        # Rows live inside one already-protected POST. A token per row would be
        # the same token repeated, for no gain.
        csrf = False

    this_period = MoneyField("Completed this period")
    stored = MoneyField("Stored to date")


class CertifyForm(FlaskForm):
    """Record what the architect certified, which may not be what was asked.

    The amount is required. Defaulting it to the amount applied for would
    quietly erase the very difference this exists to capture.
    """

    amount_certified = MoneyField("Amount certified", required=True)
    certified_by_label = StringField("Certified by", validators=[DataRequired(), Length(max=200)])
    reason = TextAreaField(
        "Reason for any difference",
        validators=[Optional(), Length(max=2000)],
        description="Required when the certified amount differs from the amount applied for.",
    )
    submit = SubmitField("Record the certificate")


class VoidForm(FlaskForm):
    reason = StringField("Reason", validators=[DataRequired(), Length(max=500)])
    submit = SubmitField("Void this application")


# ── Change orders ───────────────────────────────────────────────────────────


class ChangeOrderForm(FlaskForm):
    number = StringField("Number", validators=[DataRequired(), Length(max=64)])
    description = StringField("Description", validators=[DataRequired(), Length(max=500)])
    submit = SubmitField("Create")


class ChangeOrderLineForm(FlaskForm):
    """One line of a change order.

    Either it adjusts an existing schedule line or it creates a new one. The
    view enforces the either/or, because only the view knows which lines exist.
    """

    amount = MoneyField("Amount", required=True)
    sov_line_id = SelectField("Adjusts line", validators=[Optional()], validate_choice=False)
    new_item_no = StringField("Or create item no.", validators=[Optional(), Length(max=32)])
    description = StringField("Description", validators=[Optional(), Length(max=500)])
    csi_code = StringField("CSI division", validators=[Optional(), Length(max=16)])
    submit = SubmitField("Add line")


class ApproveChangeOrderForm(FlaskForm):
    approved_date = DateField("Approved on", validators=[DataRequired()])
    submit = SubmitField("Approve")


# ── Stored materials ────────────────────────────────────────────────────────


class StoredMaterialForm(FlaskForm):
    sov_line_id = SelectField("Schedule line", validators=[DataRequired()], validate_choice=False)
    description = StringField("Description", validators=[DataRequired(), Length(max=500)])
    value = MoneyField("Value", required=True)
    location = SelectField(
        "Location",
        choices=[
            ("onsite", "On site"),
            ("bonded_offsite", "Bonded, off site"),
            ("offsite", "Off site"),
        ],
    )
    supplier = StringField("Supplier", validators=[Optional(), Length(max=200)])
    invoice_ref = StringField("Invoice reference", validators=[Optional(), Length(max=120)])
    bond_ref = StringField(
        "Bond reference",
        validators=[Optional(), Length(max=120)],
        description="Off-site material is normally only billable when it is bonded.",
    )
    submit = SubmitField("Record the material")


# ── Payments ────────────────────────────────────────────────────────────────


class PaymentForm(FlaskForm):
    amount = MoneyField("Amount received", required=True)
    received_on = DateField("Received on", validators=[DataRequired()])
    method = SelectField(
        "Method",
        choices=[
            ("check", "Cheque"),
            ("ach", "ACH"),
            ("wire", "Wire"),
            ("joint_check", "Joint cheque"),
            ("other", "Other"),
        ],
    )
    reference = StringField("Reference", validators=[Optional(), Length(max=120)])
    joint_payee = StringField(
        "Joint payee",
        validators=[Optional(), Length(max=200)],
        description="Massing Bill records the arrangement. It does not issue the cheque.",
    )
    note = TextAreaField("Note", validators=[Optional(), Length(max=2000)])
    submit = SubmitField("Record the payment")


# ── Lien waivers ────────────────────────────────────────────────────────────


class WaiverRequestForm(FlaskForm):
    waiver_type = SelectField(
        "Type",
        choices=[
            ("conditional_progress", "Conditional - progress payment"),
            ("unconditional_progress", "Unconditional - progress payment"),
            ("conditional_final", "Conditional - final payment"),
            ("unconditional_final", "Unconditional - final payment"),
        ],
    )
    claimant_name = StringField("Claimant", validators=[DataRequired(), Length(max=200)])
    amount = MoneyField("Amount", required=True)
    through_date = DateField("Through date", validators=[DataRequired()])
    submit = SubmitField("Issue the waiver")


class VerifyTemplateForm(FlaskForm):
    """Enter the verbatim statutory text for a prescribed form.

    A bare textarea with no placeholder and no example, deliberately: the words
    have to come from the statute, and a suggestion is the one thing that must
    not appear here.
    """

    body = TextAreaField(
        "Verbatim statutory text",
        validators=[DataRequired(), Length(min=50)],
        description="Copy the exact wording from the citation. Do not paraphrase.",
    )
    submit = SubmitField("Mark verified")


class SignWaiverForm(FlaskForm):
    signer_name = StringField("Full name", validators=[DataRequired(), Length(max=200)])
    signer_title = StringField("Title", validators=[Optional(), Length(max=200)])
    signer_email = StringField("Email", validators=[DataRequired(), Email(), Length(max=254)])
    typed_signature = StringField(
        "Type your name to sign",
        validators=[DataRequired(), Length(max=200)],
        description="Typing your name here is your signature (ESIGN/UETA).",
    )
    intent = BooleanField(
        "I intend this to be my electronic signature, and I have reviewed the document above.",
        validators=[DataRequired()],
    )
    submit = SubmitField("Sign")


# ── Compliance ──────────────────────────────────────────────────────────────


class ComplianceRequirementForm(FlaskForm):
    kind = SelectField("Document", validators=[DataRequired()], validate_choice=False)
    blocks_payment = BooleanField("Missing or expired document blocks payment")
    submit = SubmitField("Add requirement")


class ComplianceDocumentForm(FlaskForm):
    kind = SelectField("Document", validators=[DataRequired()], validate_choice=False)
    reference = StringField("Reference", validators=[Optional(), Length(max=200)])
    issued_on = DateField("Issued", validators=[Optional()])
    expires_on = DateField("Expires", validators=[Optional()])
    submit = SubmitField("File the document")


# ── Subcontracts ────────────────────────────────────────────────────────────


class SubcontractForm(FlaskForm):
    number = StringField("Number", validators=[DataRequired(), Length(max=64)])
    vendor_name = StringField("Subcontractor", validators=[DataRequired(), Length(max=200)])
    vendor_email = StringField("Email", validators=[Optional(), Email(), Length(max=254)])
    amount = MoneyField("Contract amount", required=True)
    retainage_bp = BasisPointField(
        "Retainage %",
        description="Left blank, the prime contract rate applies.",
    )
    scope = TextAreaField("Scope", validators=[Optional(), Length(max=4000)])
    submit = SubmitField("Create the subcontract")


class SubApplicationForm(FlaskForm):
    amount = MoneyField("Amount this period", required=True)
    period_end = DateField("Through", validators=[DataRequired()])
    submit = SubmitField("Record the billing")


class RejectForm(FlaskForm):
    reason = StringField("Reason", validators=[DataRequired(), Length(max=500)])
    submit = SubmitField("Reject")


# ── API keys and webhooks ───────────────────────────────────────────────────


class ApiKeyForm(FlaskForm):
    name = StringField(
        "What is this key for?",
        validators=[DataRequired(), Length(max=120)],
        description="Named so it can be recognised later, and revoked without guessing.",
    )
    scopes = SelectMultipleField("Scopes", validators=[Optional()], validate_choice=False)
    submit = SubmitField("Mint the key")


class WebhookForm(FlaskForm):
    url = StringField("Endpoint URL", validators=[DataRequired(), Length(max=500)])
    description = StringField("Description", validators=[Optional(), Length(max=200)])
    secret = StringField(
        "Signing secret",
        validators=[DataRequired(), Length(min=16, max=200)],
        description="Signs every delivery. Store it on your receiving end.",
    )
    events = SelectMultipleField("Events", validators=[Optional()], validate_choice=False)
    submit = SubmitField("Add the subscription")


class VerifyDeadlineForm(FlaskForm):
    """Record a statutory day count that somebody actually read.

    The citation is required, not optional. A day count without a source is
    indistinguishable from a guess six months later, and the whole point of the
    unverified default is that guesses do not get to become dates.
    """

    days = IntegerField("Days", validators=[DataRequired()])
    citation = StringField(
        "Citation",
        validators=[DataRequired(), Length(max=500)],
        description="The section you read this number out of.",
    )
    anchor = SelectField(
        "Counts from",
        choices=[
            ("first_furnishing", "First furnishing"),
            ("last_furnishing", "Last furnishing"),
            ("substantial_completion", "Substantial completion"),
            ("notice_of_completion", "Notice of completion"),
        ],
        validators=[Optional()],
    )
    day_basis = SelectField(
        "Day basis",
        choices=[("calendar", "Calendar days"), ("business", "Business days")],
        validators=[Optional()],
    )
    submit = SubmitField("Mark verified")


__all__ = [
    "US_STATES",
    "ApiKeyForm",
    "ApproveChangeOrderForm",
    "BasisPointField",
    "CertifyForm",
    "ChangeOrderForm",
    "ChangeOrderLineForm",
    "ChangeRoleForm",
    "ComplianceDocumentForm",
    "ComplianceRequirementForm",
    "ConfirmForm",
    "ContractForm",
    "EnrolMfaForm",
    "InviteMemberForm",
    "MfaForm",
    "MoneyField",
    "OpenPeriodForm",
    "PaymentForm",
    "PeriodLineForm",
    "ProjectForm",
    "RegisterForm",
    "RejectForm",
    "SignInForm",
    "SignWaiverForm",
    "SovLineForm",
    "StoredMaterialForm",
    "SubApplicationForm",
    "SubcontractForm",
    "VerifyDeadlineForm",
    "VerifyTemplateForm",
    "VoidForm",
    "WaiverRequestForm",
    "WebhookForm",
    "cents",
]
