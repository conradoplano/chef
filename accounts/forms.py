from django import forms


class EmailLoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email", "placeholder": "you@example.com"})
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


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
