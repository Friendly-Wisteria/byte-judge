from django import forms

class ScreenshotForm(forms.Form):
    image = forms.ImageField()
