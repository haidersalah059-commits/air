from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import Airport, Passenger, Payment, SeatClass


class FlightSearchForm(forms.Form):
    origin = forms.ModelChoiceField(Airport.objects.all(), label=_("From"), empty_label="---------")
    destination = forms.ModelChoiceField(Airport.objects.all(), label=_("To"), empty_label="---------")
    date = forms.DateField(label=_("Departure date"), widget=forms.DateInput(attrs={"type": "date"}))
    seat_class = forms.ChoiceField(label=_("Class"), choices=SeatClass.choices, initial=SeatClass.ECONOMY)
    passengers = forms.IntegerField(label=_("Passengers"), min_value=1, max_value=9, initial=1)

    def __init__(self, *args, fixed_passengers=None, **kwargs):
        super().__init__(*args, **kwargs)
        if fixed_passengers:
            # Every leg of one booking carries the same passengers.
            self.fields["passengers"].disabled = True
            self.fields["passengers"].initial = fixed_passengers

    def clean(self):
        data = super().clean()
        if data.get("origin") and data.get("origin") == data.get("destination"):
            raise forms.ValidationError(_("Origin and destination must be different."))
        if data.get("date") and data["date"] < timezone.localdate():
            raise forms.ValidationError(_("The date is in the past."))
        return data


class PassengerForm(forms.Form):
    first_name = forms.CharField(label=_("first name"), max_length=60)
    last_name = forms.CharField(label=_("last name"), max_length=60)
    passport_no = forms.CharField(label=_("passport number"), max_length=20)
    nationality = forms.CharField(label=_("nationality"), max_length=60)
    birth_date = forms.DateField(label=_("date of birth"), required=False,
                                 widget=forms.DateInput(attrs={"type": "date"}))
    email = forms.EmailField(label=_("email"), required=False)
    phone = forms.CharField(label=_("phone"), max_length=30, required=False)

    def __init__(self, *args, legs=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.legs = legs
        for index, leg in enumerate(legs):
            self.fields[f"seat_{index}"] = forms.ChoiceField(
                label=_("Seat on %(flight)s") % {"flight": leg["label"]},
                choices=[("", "---------")] + leg["seat_choices"])

    @property
    def seat_fields(self):
        return [self[f"seat_{i}"] for i in range(len(self.legs))]

    @property
    def person_fields(self):
        return [self[name] for name in ("first_name", "last_name", "passport_no", "nationality", "birth_date",
                                        "email", "phone")]

    def clean_passport_no(self):
        return self.cleaned_data["passport_no"].strip().upper()

    def save_passenger(self):
        data = self.cleaned_data
        passenger, _created = Passenger.objects.update_or_create(
            passport_no=data["passport_no"],
            defaults={k: data[k] for k in ("first_name", "last_name", "nationality", "birth_date", "email", "phone")},
        )
        return passenger


class BasePassengerFormSet(forms.BaseFormSet):
    def clean(self):
        if any(self.errors):
            return
        passports = [f.cleaned_data["passport_no"] for f in self.forms]
        if len(passports) != len(set(passports)):
            raise forms.ValidationError(_("Each passenger must have a different passport number."))
        for index, leg in enumerate(self.forms[0].legs if self.forms else []):
            seats = [f.cleaned_data[f"seat_{index}"] for f in self.forms]
            if len(seats) != len(set(seats)):
                raise forms.ValidationError(_("Two passengers chose the same seat on %(flight)s.")
                                            % {"flight": leg["label"]})


PassengerFormSet = forms.formset_factory(PassengerForm, formset=BasePassengerFormSet, extra=0)


class ContactForm(forms.Form):
    contact_email = forms.EmailField(label=_("contact email"),
                                     help_text=_("The confirmation and e-tickets are sent here."))


class PaymentForm(forms.Form):
    method = forms.ChoiceField(label=_("method"), choices=Payment.Method.choices)
    reference = forms.CharField(label=_("reference"), max_length=40, required=False,
                                help_text=_("Receipt or transaction number. Leave empty to generate a test one."))
