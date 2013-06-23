from __future__ import unicode_literals

import logging
import warnings

from datetime import timedelta

try:
    from urllib.parse import urlparse
except ImportError:  # python2
    from urlparse import urlparse

import requests

from django.conf import settings
from django.core.urlresolvers import reverse
from django.db import models
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _

from .utils import (get_hub, get_hub_credentials, generate_random_string,
                    get_domain)

logger = logging.getLogger(__name__)


class SubscriptionError(Exception):
    pass


class SubscriptionManager(models.Manager):
    def subscribe(self, topic, hub=None, lease_seconds=None):
        if hub is None:
            warnings.warn("Subscribing without providing the hub is "
                          "deprecated.", DeprecationWarning)
            hub = get_hub(topic)

        # Only use a secret over HTTPS
        scheme = urlparse(hub).scheme
        defaults = {}
        if scheme == 'https':
            defaults['secret'] = generate_random_string()

        subscription, created = self.get_or_create(hub=hub, topic=topic,
                                                   defaults=defaults)
        subscription.subscribe(lease_seconds=lease_seconds)
        return subscription

    def unsubscribe(self, topic, hub=None):
        warnings.warn("The unsubscribe manager method is deprecated and is "
                      "now available as a method on the subscription instance "
                      "directly.", DeprecationWarning)
        if hub is None:
            hub = get_hub(topic)

        try:
            subscription = Subscription.objects.get(topic=topic, hub=hub)
        except self.model.DoesNotExist:
            return

        subscription.unsubscribe()


class Subscription(models.Model):
    hub = models.URLField(_('Hub'), max_length=1023)
    topic = models.URLField(_('Topic'), max_length=1023)
    verified = models.BooleanField(_('Verified'), default=False)
    verify_token = models.CharField(_('Verify Token'), max_length=255,
                                    blank=True)
    lease_expiration = models.DateTimeField(_('Lease expiration'),
                                            null=True, blank=True)
    secret = models.CharField(_('Secret'), max_length=255, blank=True)

    objects = SubscriptionManager()

    def __unicode__(self):
        return '%s: %s' % (self.topic, self.hub)

    def set_expiration(self, seconds):
        self.lease_expiration = timezone.now() + timedelta(seconds=seconds)

    def has_expired(self):
        if self.lease_expiration:
            return timezone.now() > self.lease_expiration
        return False

    @property
    def callback_url(self):
        callback_url = reverse('subscriber_callback', args=[self.pk])
        use_ssl = getattr(settings, 'PUSH_SSL_CALLBACK', False)
        scheme = 'https' if use_ssl else 'http'
        return '%s://%s%s' % (scheme, get_domain(), callback_url)

    def subscribe(self, lease_seconds=None):
        return self.send_request(mode='subscribe', lease_seconds=lease_seconds)

    def unsubscribe(self):
        return self.send_request(mode='unsubscribe')

    def send_request(self, mode, lease_seconds=None):
        params = {
            'hub.mode': mode,
            'hub.callback': self.callback_url,
            'hub.topic': self.topic,
            'hub.verify': ['sync', 'async'],
        }

        if self.secret:
            params['hub.secret'] = self.secret

        if lease_seconds is None:
            lease_seconds = getattr(settings, 'PUSH_LEASE_SECONDS', None)

        # If not provided, let the hub decide.
        if lease_seconds is not None:
            params['hub.lease_seconds'] = lease_seconds

        credentials = get_hub_credentials(self.hub)
        response = requests.post(self.hub, data=params, auth=credentials)

        if response.status_code in (202, 204):
            if (
                mode == 'subscribe' and
                response.status_code == 204  # synchronous verification (0.3)
            ):
                self.verified = True
                Subscription.objects.filter(pk=self.pk).update(verified=True)

            elif response.status_code == 202:
                if mode == 'unsubscribe':
                    self.pending_unsubscription = True
                    # TODO check for making sure unsubscriptions are legit
                    #Subscription.objects.filter(pk=self.pk).update(
                    #    pending_unsubscription=True)
            return response

        raise SubscriptionError(
            "Error while subscribing to topic {0} via hub {1}: {2}".format(
                self.topic, self.hub, response.text),
            self,
            response,
        )
