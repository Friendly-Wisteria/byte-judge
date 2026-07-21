from django import forms

class JobOfferRiskAssessForm(forms.Form):
    image = forms.ImageField(required=False)
    text = forms.CharField(required=False, widget=forms.Textarea)

    def clean(self):
        cleaned_data = super().clean()
        image = cleaned_data.get('image')
        text = cleaned_data.get('text')  # CharFieldは既定でstrip済み
        if not image and not text:
            raise forms.ValidationError('スクショまたは募集文のどちらかを入力してください。')
        return cleaned_data
