from django import forms


class EmailLoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email", "placeholder": "you@example.com"})
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class RegisterForm(forms.Form):
    name = forms.CharField(label="Your name", max_length=150, widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "name"}))
    email = forms.EmailField(widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "you@example.com"}))
    household = forms.CharField(
        label="Household name", max_length=100, required=False,
        widget=forms.TextInput(attrs={"placeholder": "E.g. The Smiths"}),
        help_text="Optional. You can add the others who plan and shop with you later.",
    )

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean_household(self):
        return " ".join(self.cleaned_data["household"].split())


class CodeForm(forms.Form):
    code = forms.RegexField(
        regex=r"^\d{6}$",
        label="Six-digit code",
        error_messages={"invalid": "Enter the six digits from the email."},
        widget=forms.TextInput(
            attrs={
                "autofocus": True,
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
                "maxlength": 6,
                "placeholder": "123456",
            }
        ),
    )

    def clean_code(self):
        return self.cleaned_data["code"].strip()
