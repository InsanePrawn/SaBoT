import datetime
import json
import random
import string

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.core.mail import send_mail
from django.core.urlresolvers import reverse
from django.db import transaction
from django.http import HttpResponseRedirect, Http404, HttpResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.views.generic.base import TemplateResponseMixin, TemplateView
from django.views.generic.detail import SingleObjectTemplateResponseMixin, SingleObjectMixin
from django.views.generic.edit import BaseFormView, UpdateView
from django.views.generic import FormView, CreateView

from rtkit.authenticators import BasicAuthenticator
from rtkit.resource import RTResource
from rtkit.errors import RTResourceError


from sabot.views import ChangeNotificationMixin, PermCheckUpdateView, JobProcessingView
from account.models import UserProfile
from forms import SponsorCreationForm, SponsorForm, SponsorMailSelectorForm, SponsorParcelForm, SponsorParcelAdminForm, SponsorParcelAdminInlineCreateForm
from models import Sponsoring, SponsorContact, SponsorParcel


def id_generator(size=6, chars=string.ascii_lowercase + string.digits):
	return ''.join(random.choice(chars) for x in range(size))


class ParcelCreateView(CreateView):
	model = SponsorParcel
	template_name = "sponsor/parcel/update.html"

	def __init__(self, *args, **kwargs):
		super(ParcelCreateView, self).__init__(*args,**kwargs)
		self.sponsoring = None

	def get_form_class(self):
		return SponsorParcelAdminInlineCreateForm if self.request.user.is_staff else SponsorParcelForm

	def get_sponsoring(self):
		if self.sponsoring is None:
			try:
				self.sponsoring = Sponsoring.objects.get(pk=self.kwargs["spk"])
			except Sponsoring.DoesNotExist:
				raise Http404
		return self.sponsoring

	def form_valid(self, form):
		self.object = form.save(commit=False)
		self.object.sponsoring = self.get_sponsoring()
		self.object.save()
		return redirect(self.get_success_url())

	def get_success_url(self):
		return reverse("sponsor_update", kwargs = { "pk" : self.kwargs["spk"] }) + "#parcel"

class ParcelUpdateView(PermCheckUpdateView):
	model = SponsorParcel
	template_name = "sponsor/parcel/update.html"

	def get_form_class(self):
		return SponsorParcelAdminForm if self.request.user.is_staff else SponsorParcelForm

	def get_success_url(self):
		if self.object.sponsoring is not None:
			return reverse("sponsor_update", kwargs = { "pk" : self.object.sponsoring_id }) + "#parcel"
		else:
			return reverse("parcel_list")

class SponsorEmailingView(FormView):
	form_class = SponsorMailSelectorForm
	template_name = "sponsor/contactMailer/mailform.html"
	success_template_name = "sponsor/contactMailer/mailformResults.html"

	def get_context_data(self, **kwargs):
		kwargs = super(SponsorEmailingView, self).get_context_data(**kwargs)
		kwargs["contact_list"] = SponsorContact.objects.select_related()
		return kwargs

	def form_valid(self, form):
		# initialize rt connection
		rt = RTResource(settings.RT_URL, settings.RT_USER, settings.RT_PW, BasicAuthenticator)

		results = []
		for contact in form.cleaned_data["recipients"]:
			result = {}
			result["contact"] = contact

			# now compose the mail and send it as rt answer
			tmpl = contact.template
			ctx_dict = { "rcpt" : contact }
			message = render_to_string(tmpl.templateName, ctx_dict)
			# create response
			resp_data = {
				"content" : {
					"Action" : "correspond",
					"Text" : message.encode("utf8"),
					"Status" : "stalled",
				}
			}
			# handle attachments
			if len(tmpl.attachments.all()) > 0:
				attId = 1
				names = []
				for att in tmpl.attachments.all():
					names.append(att.name)
					resp_data["attachment_{0}".format(attId)] = file(settings.ATTACHMENTS_ROOT + att.filepath)
					attId = attId + 1

				resp_data["content"]["Attachment"] = ", ".join(names)

			# post it to rt

			# create a matching ticket
			data = {
				"content" : {
					"Queue" : settings.RT_QUEUE,
					"Owner" : settings.RT_TICKET_OWNER,
					"Subject" : contact.template.mailSubject,
					"Requestor" : (contact.contactPersonEmail if len(contact.contactPersonEmail) > 0 else contact.contactEMail),
				}
			}
			res = rt.post(path="ticket/new",payload=data)
			if res.status_int != 200:
				result["status"] = "failed"
				result["info"] = res.status
				results.append(result)
				continue

			tpath = None
			tid = None
			for k,v in res.parsed[0]:
				if k == "id":
					tpath = v
					break

			if tpath is None or tpath.find("/") < 0:
				result["status"] = "failed"
				result["info"] = "Answer contained no ticket id"
				results.append(result)
				continue
			else:
				tid = int(tpath.split("/")[1])

			res = rt.post(path=(tpath + "/comment"),payload=resp_data)
			if res.status_int != 200:
				result["status"] = "failed"
				result["info"] = repr(res.parded)
				results.append(result)
				continue

			result["status"] = "ok"
			results.append(result)
			contact.lastMailed = datetime.date.today()
			contact.rtTicketId = tid
			contact.save()


		self.template_name = self.success_template_name
		return self.render_to_response(self.get_context_data(results=results))


def sponsorMailPreview(request, pk):
	resp = {}
	resp["success"] = False
	try:
		contact = SponsorContact.objects.get(pk=pk)
		resp["company"] = contact.companyName
		tmpl = contact.template
		if tmpl is not None:
			ctx_dict = { "rcpt" : contact }
			message = render_to_string(tmpl.templateName, ctx_dict)
			resp["message"] = message.encode("utf8")
			if len(tmpl.attachments.all()) > 0:
				names = []
				for att in tmpl.attachments.all():
					names.append(att.name)

				resp["attachments"] = names

			resp["success"] = True

	except SponsorContact.DoesNotExist:
		pass

	return HttpResponse(json.dumps(resp), content_type="application/json")

class SponsorCreateView(FormView):
	form_class = SponsorCreationForm
	template_name = "sponsor/sponsoring/create.html"
	success_url = "../%(id)s"

	def form_valid(self, form):
		# create a new user for this sponsor
		try:
			sp = transaction.savepoint()
			user = User(username = form.cleaned_data["sponsorUsername"])
			baseContact = form.cleaned_data["sponsorContact"]
			user.first_name = baseContact.contactPersonFirstname
			user.last_name = baseContact.contactPersonSurname
			user.email = baseContact.contactPersonEmail
			user.save()

			profile = UserProfile(user = user)
			profile.authToken = id_generator(24)
			profile.save()

			sponsoring = Sponsoring()
			sponsoring.owner = user
			sponsoring.contact = baseContact
			sponsoring.package = form.cleaned_data["sponsorPackage"]
			sponsoring.adminComment = form.cleaned_data["internalComment"]
			sponsoring.save()

			self.object = sponsoring

			transaction.savepoint_commit(sp)
		except Exception as e:
			transaction.savepoint_rollback(sp)
			raise e


		return HttpResponseRedirect(self.get_success_url())

	def get_context_data(self, **kwargs):
		context = super(SponsorCreateView, self).get_context_data(**kwargs)
		try:
			sponsorContact = SponsorContact.objects.get(pk=self.kwargs["pk"])
		except SponsorContact.DoesNotExist:
			raise Http404
		context["sponsorContact"] = sponsorContact
		return context


	def get_success_url(self):
		if self.success_url:
			url = self.success_url % self.object.__dict__
		else:
			raise ImproperlyConfigured("No URL to redirect to")
		return url


class SponsorUpdateView(ChangeNotificationMixin,PermCheckUpdateView):
	model = Sponsoring
	form_class = SponsorForm
	template_name = "sponsor/sponsoring/update.html"

	def get_context_data(self, **kwargs):
		kwargs["object"] = self.object
		return super(SponsorUpdateView, self).get_context_data(**kwargs)

	def get_form_kwargs(self):
		kwargs = super(SponsorUpdateView, self).get_form_kwargs()
		kwargs["request"] = self.request
		return kwargs

	def get_success_url(self):
		return reverse("sponsor_update", kwargs = { "pk" : self.object.pk })

	def change_notification(self, changed_fields):
		if len(changed_fields) == 0:
			return
		change_dict = {}
		for field in changed_fields:
			field_name = self.object.fieldDescriptionalNames.get(field,field)
			change_dict[field_name] = getattr(self.object,field)

		ctx_dict = {
			"sponsor" : self.object,
			"change_dict" : change_dict,
			"user" : self.request.user,
		}
		subject = "Sponsor information change notification"
		message = render_to_string('sponsor/change_notification_email.txt', ctx_dict)
		send_mail(subject,message,settings.DEFAULT_FROM_EMAIL,settings.SPONSOR_NOTIFICATION,fail_silently=True)

		if "clearedForBilling" in changed_fields and self.object.clearedForBilling == True:
			subject = "Sponsoring ready for billing"
			message = render_to_string('sponsor/ready_for_billing_notification_email.txt', ctx_dict)
			send_mail(subject,message,settings.DEFAULT_FROM_EMAIL,settings.FINANCE_EMAIL,fail_silently=True)

class SponsorContactResetEmailView(JobProcessingView,TemplateView):
	def process_job(self):
		SponsorContact.objects.all().update(responded=False, lastMailed=None, rtTicketId=None)
		return True

def respond_json(jdata):
	return HttpResponse(json.dumps(jdata), content_type="application/json")


def loadResponseInfoFromRT(request):
	if request.method != "POST":
		return HttpResponseNotAllowed(["POST"])

	rt = RTResource(settings.RT_URL, settings.RT_USER, settings.RT_PW, BasicAuthenticator)
	res = { "failed" : 0, "succeded" : 0 , "updated" : 0 }

	unansweredContacts = SponsorContact.objects.filter(responded=False).exclude(rtTicketId=None)
	for contact in unansweredContacts:
		# read the ticket from RT
		rtres = rt.get(path="ticket/{}/history".format(contact.rtTicketId))
		if rtres.status_int != 200:
			res["failed"] = res["failed"] + 1
			continue
		res["succeded"] = res["succeded"] + 1
		results = rtres.parsed
		if (len(
				[r for r in rtres.parsed[0] if
					r[1].lower().find("correspondence added") >= 0 and
					r[1].find("@") >= 0])
					>= 1):

			# count the number of correspondances that also contain an @. corresespondances added with "@" come from external mail and not rt user?!
			res["updated"] = res["updated"] + 1
			contact.responded = True
			contact.save()

	return respond_json(res)

