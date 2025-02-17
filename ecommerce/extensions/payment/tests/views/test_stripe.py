import json
from ast import literal_eval

import stripe
from ddt import ddt, file_data
from django.conf import settings
from django.urls import reverse
from mock import mock
from oscar.core.loading import get_class, get_model
from rest_framework import status

from ecommerce.core.constants import (
    COURSE_ENTITLEMENT_PRODUCT_CLASS_NAME,
    ENROLLMENT_CODE_PRODUCT_CLASS_NAME,
    SEAT_PRODUCT_CLASS_NAME
)
from ecommerce.courses.tests.factories import CourseFactory
from ecommerce.entitlements.utils import create_or_update_course_entitlement
from ecommerce.extensions.basket.constants import PAYMENT_INTENT_ID_ATTRIBUTE
from ecommerce.extensions.basket.utils import basket_add_payment_intent_id_attribute, get_basket_courses_list
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.order.constants import PaymentEventTypeName
from ecommerce.extensions.payment.constants import STRIPE_CARD_TYPE_MAP
from ecommerce.extensions.payment.processors.stripe import Stripe
from ecommerce.extensions.payment.tests.mixins import PaymentEventsMixin
from ecommerce.extensions.test.factories import create_basket
from ecommerce.tests.testcases import TestCase

BasketAttribute = get_model('basket', 'BasketAttribute')
BasketAttributeType = get_model('basket', 'BasketAttributeType')
Country = get_model('address', 'Country')
Order = get_model('order', 'Order')
PaymentEvent = get_model('order', 'PaymentEvent')
Selector = get_class('partner.strategy', 'Selector')
Source = get_model('payment', 'Source')
Product = get_model('catalogue', 'Product')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')

STRIPE_TEST_FIXTURE_PATH = 'ecommerce/extensions/payment/tests/views/fixtures/test_stripe_test_payment_flow.json'


@ddt
class StripeCheckoutViewTests(PaymentEventsMixin, TestCase):
    path = reverse('stripe:submit')

    def setUp(self):
        super(StripeCheckoutViewTests, self).setUp()
        self.user = self.create_user()
        self.client.login(username=self.user.username, password=self.password)
        self.site.siteconfiguration.client_side_payment_processor = 'stripe'
        self.site.siteconfiguration.save()
        Country.objects.create(iso_3166_1_a2='US', name='US')
        self.mock_enrollment_api_resp = mock.Mock()
        self.mock_enrollment_api_resp.status_code = status.HTTP_200_OK

        self.stripe_checkout_url = reverse('stripe:checkout')
        self.capture_context_url = reverse('bff:payment:v0:capture_context')

    def payment_flow_with_mocked_stripe_calls(
            self,
            url,
            data,
            create_side_effect=None,
            retrieve_side_effect=None,
            confirm_side_effect=None,
            modify_side_effect=None,
            in_progress_payment=None):
        """
        Helper function to mock all stripe calls with successful responses.

        Useful for when you want to mock something else without a wall
        of context managers in your test.
        """
        # Requires us to run tests from repo root directory. Too fragile?
        with open(STRIPE_TEST_FIXTURE_PATH, 'r') as fixtures:  # pylint: disable=unspecified-encoding
            fixture_data = json.load(fixtures)['happy_path']

        # hit capture_context first
        with mock.patch('stripe.PaymentIntent.create') as mock_create:
            if create_side_effect is not None:
                mock_create.side_effect = create_side_effect
            else:
                mock_create.side_effect = [fixture_data['create_resp']]
            self.client.get(self.capture_context_url)

        # now hit POST endpoint
        with mock.patch('stripe.PaymentIntent.retrieve') as mock_retrieve:
            if retrieve_side_effect is not None:
                mock_retrieve.side_effect = retrieve_side_effect
            else:
                mock_retrieve.side_effect = [fixture_data['retrieve_addr_resp']]

            with mock.patch(
                'ecommerce.extensions.fulfillment.modules.EnrollmentFulfillmentModule._post_to_enrollment_api'
            ) as mock_api_resp:
                mock_api_resp.return_value = self.mock_enrollment_api_resp

                with mock.patch('stripe.PaymentIntent.confirm') as mock_confirm:
                    if confirm_side_effect is not None:
                        mock_confirm.side_effect = confirm_side_effect
                    elif in_progress_payment is not None:
                        mock_confirm.side_effect = [fixture_data['confirm_resp_in_progress']]
                    else:
                        mock_confirm.side_effect = [fixture_data['confirm_resp']]
                    with mock.patch('stripe.PaymentIntent.modify') as mock_modify:
                        if modify_side_effect is not None:
                            mock_modify.side_effect = modify_side_effect
                        else:
                            mock_modify.side_effect = [fixture_data['modify_resp']]
                        # make your call
                        return self.client.post(
                            url,
                            data=data
                        )

    def assert_successful_order_response(self, response, order_number):
        assert response.status_code == 201
        receipt_url = get_receipt_page_url(
            self.request,
            self.site_configuration,
            order_number,
            disable_back_button=True
        )
        assert response.json() == {'url': receipt_url}

    def assert_order_created(self, basket, billing_address, card_type, label):
        order = Order.objects.get(number=basket.order_number, total_incl_tax=basket.total_incl_tax)
        total = order.total_incl_tax
        order.payment_events.get(event_type__code='paid', amount=total)
        Source.objects.get(
            source_type__name=Stripe.NAME,
            currency=order.currency,
            amount_allocated=total,
            amount_debited=total,
            card_type=STRIPE_CARD_TYPE_MAP[card_type],
            label=label
        )
        PaymentEvent.objects.get(
            event_type__name=PaymentEventTypeName.PAID,
            amount=total,
            processor_name=Stripe.NAME
        )
        assert order.billing_address == billing_address

    def create_basket(self, product_class=None):
        basket = create_basket(owner=self.user, site=self.site, product_class=product_class)
        basket.strategy = Selector().strategy()
        basket.thaw()
        basket.flush()
        course = CourseFactory()
        seat = course.create_or_update_seat('credit', False, 100, 'credit_provider_id', None, 2)
        basket.add_product(seat, 1)
        return basket

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self.path)
        expected_url = '{base}?next={path}'.format(base=reverse(settings.LOGIN_URL), path=self.path)
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)

    @file_data('fixtures/test_stripe_test_payment_flow.json')
    def test_payment_flow(
            self,
            confirm_resp,
            confirm_resp_in_progress,  # pylint: disable=unused-argument
            create_resp,
            modify_resp,
            cancel_resp,  # pylint: disable=unused-argument
            refund_resp,  # pylint: disable=unused-argument
            retrieve_addr_resp,
            retrieve_resp_in_progress):  # pylint: disable=unused-argument
        """
        Verify that the stripe payment flow, hitting capture-context and
        stripe-checkout urls, results in a basket associated with the correct
        stripe payment_intent_id, and a processor response is recorded.

        Args:
            confirm_resp: Response for confirm call on payment purchase
            create_resp: Response for create call when capturing context
            modify_resp: Response for modify call before confirming response
            retrieve_addr_resp: Response for retrieve call that should be made when getting billing address
            confirm_resp: Response for confirm call that should be made when handling processor response
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)
        idempotency_key = f'basket_pi_create_v1_{basket.order_number}'

        # need to call capture-context endpoint before we call do GET to the stripe checkout view
        # so that the PaymentProcessorResponse is already created
        with mock.patch('stripe.PaymentIntent.create') as mock_create:
            mock_create.return_value = create_resp
            self.client.get(self.capture_context_url)
            mock_create.assert_called_once()
            assert mock_create.call_args.kwargs['idempotency_key'] == idempotency_key
            courses_metadata_list = literal_eval(mock_create.call_args.kwargs['metadata']['courses'])
            assert len(courses_metadata_list) == basket.lines.count()
            assert courses_metadata_list[0]['course_id'] == basket.lines.first().product.course.id
            assert courses_metadata_list[0]['course_name'] == basket.lines.first().product.course.name

        with mock.patch('stripe.PaymentIntent.retrieve') as mock_retrieve:
            mock_retrieve.return_value = retrieve_addr_resp

            with mock.patch(
                'ecommerce.extensions.fulfillment.modules.EnrollmentFulfillmentModule._post_to_enrollment_api'
            ) as mock_api_resp:
                mock_api_resp.return_value = self.mock_enrollment_api_resp

                with mock.patch('stripe.PaymentIntent.confirm') as mock_confirm:
                    mock_confirm.return_value = confirm_resp
                    with mock.patch('stripe.PaymentIntent.modify') as mock_modify:
                        mock_modify.return_value = modify_resp
                        self.client.post(
                            self.stripe_checkout_url,
                            data={
                                'payment_intent_id': create_resp['id'],
                                'skus': basket.lines.first().stockrecord.partner_sku,
                                'dynamic_payment_methods_enabled': 'false',
                            },
                        )
                assert mock_retrieve.call_count == 1
                assert mock_modify.call_count == 1
                assert mock_confirm.call_count == 1

        # Verify BillingAddress was set correctly
        basket.refresh_from_db()
        order = basket.order_set.first()
        assert str(order.billing_address) == "Test User, 123 Test St, Sample, MA, 12345"

        # Verify there is 1 and only 1 Basket Attribute with the payment_intent_id
        # associated with our basket.
        assert BasketAttribute.objects.filter(
            value_text='pi_3LsftNIadiFyUl1x2TWxaADZ',
            basket=basket,
        ).count() == 1

        pprs = PaymentProcessorResponse.objects.filter(
            transaction_id="pi_3LsftNIadiFyUl1x2TWxaADZ"
        )
        # created when handle_processor_response is successful
        assert pprs.count() == 1
        self.assert_processor_response_recorded(
            Stripe.NAME,
            confirm_resp['id'],
            confirm_resp,
            basket=basket
        )

    def test_capture_context_basket_change(self):
        """
        Verify that existing payment intent is retrieved,
        and that we do not error with an IdempotencyError in this case: capture
        context is called to generate stripe elements, but then user backs out from
        payment page, and tries to check out with a different things in the basket.
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)
        idempotency_key = f'basket_pi_create_v1_{basket.order_number}'

        with mock.patch('stripe.PaymentIntent.create') as mock_create:
            mock_create.return_value = {
                'id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                'client_secret': 'pi_3LsftNIadiFyUl1x2TWxaADZ_secret_VxRx7Y1skyp0jKtq7Gdu80Xnh',
            }
            self.client.get(self.capture_context_url)
            mock_create.assert_called_once()
            assert mock_create.call_args.kwargs['idempotency_key'] == idempotency_key

        # Verify there is 1 and only 1 Basket Attribute with the payment_intent_id
        # associated with our basket.
        assert BasketAttribute.objects.filter(
            value_text='pi_3LsftNIadiFyUl1x2TWxaADZ',
            basket=basket,
        ).count() == 1

        # Change the basket price
        basket.flush()
        course = CourseFactory()
        seat = course.create_or_update_seat('credit', False, 99, 'credit_provider_id', None, 2)
        basket.add_product(seat, 1)
        basket.save()

        with mock.patch('stripe.PaymentIntent.create') as mock_create:
            mock_create.side_effect = stripe.error.IdempotencyError

            with mock.patch('stripe.PaymentIntent.retrieve') as mock_retrieve:
                mock_retrieve.return_value = {
                    'id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                    'client_secret': 'pi_3LsftNIadiFyUl1x2TWxaADZ_secret_VxRx7Y1skyp0jKtq7Gdu80Xnh',
                }
                self.client.get(self.capture_context_url)
                mock_retrieve.assert_called_once()
                assert mock_retrieve.call_args.kwargs['id'] == 'pi_3LsftNIadiFyUl1x2TWxaADZ'

    def test_capture_context_basket_price_change(self):
        """
        Verify that when capture-context is hit, if the basket has a pre-existing Payment Intent,
        we keep the Payment Intent updated in case the contents of the basket has changed, especially the amount.
        """
        # Create a basket with an existing Payment Intent
        payment_intent_id = 'pi_3LsftNIadiFyUl1x2TWxaADZ'
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)
        basket_add_payment_intent_id_attribute(basket, payment_intent_id)

        # Hit the capture-context endpoint where the basket already has a Payment Intent
        # and should make a modify call to Stripe.
        with mock.patch('stripe.PaymentIntent.create') as mock_create:
            with mock.patch('stripe.PaymentIntent.retrieve') as mock_retrieve:
                mock_retrieve.return_value = {
                    'id': payment_intent_id,
                    'client_secret': 'pi_3LsftNIadiFyUl1x2TWxaADZ_secret_VxRx7Y1skyp0jKtq7Gdu80Xnh',
                    'status': 'requires_payment_method'
                }
                with mock.patch('stripe.PaymentIntent.modify') as mock_modify:
                    mock_modify.return_value = {
                        'id': payment_intent_id,
                        'client_secret': 'pi_3LsftNIadiFyUl1x2TWxaADZ_secret_VxRx7Y1skyp0jKtq7Gdu80Xnh',
                        'status': 'requires_payment_method',
                        'amount': basket.total_incl_tax
                    }
                    courses = get_basket_courses_list(basket)
                    courses_metadata = str(courses)[:499] if courses else None
                    payment_intent_parameters = {
                        'amount': str((basket.total_incl_tax * 100).to_integral_value()),
                        'currency': basket.currency,
                        'description': basket.order_number,
                        'metadata': {
                            'order_number': basket.order_number,
                            'courses': courses_metadata,
                        },
                    }

                    self.client.get(self.capture_context_url)
                    mock_create.assert_not_called()
                    mock_retrieve.assert_called_once()
                    mock_modify.assert_called_once_with(payment_intent_id, **payment_intent_parameters)
                    assert mock_retrieve.call_args.kwargs['id'] == payment_intent_id

    def test_capture_context_empty_basket(self):
        basket = create_basket(owner=self.user, site=self.site)
        basket.flush()

        with mock.patch('stripe.PaymentIntent.create') as mock_create:
            mock_create.return_value = {
                'id': '',
                'client_secret': '',
            }

            self.assertTrue(basket.is_empty)
            response = self.client.get(self.capture_context_url)

            mock_create.assert_not_called()
            self.assertDictEqual(response.json(), {
                'capture_context': {
                    'key_id': mock_create.return_value['client_secret'],
                    'order_id': basket.order_number,
                }
            })
            self.assertEqual(response.status_code, 200)

    def test_capture_context_bulk_basket(self):
        """
        Verify Payment Intent metadata contains course information for bulk baskets with multiple courses.
        """
        # Create basket with multiple enrollment code products
        course = CourseFactory(partner=self.partner)
        course.create_or_update_seat('verified', True, 50, create_enrollment_code=True)
        enrollment_code = Product.objects.get(product_class__name=ENROLLMENT_CODE_PRODUCT_CLASS_NAME)
        basket = self.create_basket(product_class=ENROLLMENT_CODE_PRODUCT_CLASS_NAME)
        basket.add_product(enrollment_code, quantity=1)

        with mock.patch('stripe.PaymentIntent.create') as mock_create:
            mock_create.return_value = {
                'id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                'client_secret': 'pi_3LsftNIadiFyUl1x2TWxaADZ_secret_VxRx7Y1skyp0jKtq7Gdu80Xnh',
            }
            self.client.get(self.capture_context_url)
            mock_create.assert_called_once()
            courses_metadata_list = literal_eval(mock_create.call_args.kwargs['metadata']['courses'])
            assert len(courses_metadata_list) == basket.lines.count()
            for index, line in enumerate(basket.lines.all()):
                assert courses_metadata_list[index]['course_id'] == line.product.course.id
                assert courses_metadata_list[index]['course_name'] == line.product.course.name

    def test_capture_context_program_basket(self):
        """
        Verify Payment Intent metadata contains product title information for entitlements.
        """
        # Create basket with multiple entitlements
        entitlement_basket = create_basket(
            owner=self.user, site=self.site, product_class=COURSE_ENTITLEMENT_PRODUCT_CLASS_NAME
        )
        entitlement = create_or_update_course_entitlement(
            'verified', 100, self.partner, 'test-course-uuid', 'Course Entitlement')
        entitlement_basket.add_product(entitlement)

        with mock.patch('stripe.PaymentIntent.create') as mock_create:
            mock_create.return_value = {
                'id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                'client_secret': 'pi_3LsftNIadiFyUl1x2TWxaADZ_secret_VxRx7Y1skyp0jKtq7Gdu80Xnh',
            }
            self.client.get(self.capture_context_url)
            mock_create.assert_called_once()
            courses_metadata_list = literal_eval(mock_create.call_args.kwargs['metadata']['courses'])
            assert len(courses_metadata_list) == entitlement_basket.lines.count()
            # The product in the basket does not have a course associated to it, so no course.id and course.name
            for index, line in enumerate(entitlement_basket.lines.all()):
                assert courses_metadata_list[index]['course_id'] is None
                assert courses_metadata_list[index]['course_name'] == line.product.title

    def test_capture_context_large_characters_basket(self):
        """
        Verify we don't send Stripe metadata value that is longer than 500 characters.
        """
        # Create basket with courses that will result in courses list > 500 characters
        basket = self.create_basket()
        very_long_course_name = 'a' * 200
        course_1 = CourseFactory(id='edX/DemoX/Demo_Course_1', name=very_long_course_name, partner=self.partner)
        product = course_1.create_or_update_seat('verified', False, 50)
        basket.add_product(product)
        course_2 = CourseFactory(id='edX/DemoX/Demo_Course_2', name=very_long_course_name, partner=self.partner)
        product = course_2.create_or_update_seat('verified', False, 100)
        basket.add_product(product)

        with mock.patch('stripe.PaymentIntent.create') as mock_create:
            mock_create.return_value = {
                'id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                'client_secret': 'pi_3LsftNIadiFyUl1x2TWxaADZ_secret_VxRx7Y1skyp0jKtq7Gdu80Xnh',
            }
            self.client.get(self.capture_context_url)
            mock_create.assert_called_once()
            # The metadata must be less than 500 characters
            assert len(mock_create.call_args.kwargs['metadata']['courses']) < 500

    @file_data('fixtures/test_stripe_test_payment_flow.json')
    def test_capture_context_confirmable_status(
            self,
            confirm_resp,  # pylint: disable=unused-argument
            confirm_resp_in_progress,  # pylint: disable=unused-argument
            create_resp,  # pylint: disable=unused-argument
            modify_resp,  # pylint: disable=unused-argument
            cancel_resp,  # pylint: disable=unused-argument
            refund_resp,  # pylint: disable=unused-argument
            retrieve_addr_resp,
            retrieve_resp_in_progress):  # pylint: disable=unused-argument
        """
        Verify that hitting capture-context with a Payment Intent that already exists and it's in a status that
        can be confirmed, that a new Payment Intent is not created for this basket.
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)
        payment_intent_id = retrieve_addr_resp['id']
        basket_add_payment_intent_id_attribute(basket, payment_intent_id)

        with mock.patch('stripe.PaymentIntent.retrieve') as mock_retrieve:
            mock_retrieve.return_value = retrieve_addr_resp
            # If Payment Intent already exists for this basket, and it's in a usable status that
            # can later be confirmed, make sure we do not cancel and create a new Payment Intent.
            with mock.patch('stripe.PaymentIntent.cancel') as mock_cancel:
                self.client.get(self.capture_context_url)
                mock_cancel.assert_not_called()
                payment_intent_id = BasketAttribute.objects.get(
                    basket=basket,
                    attribute_type__name=PAYMENT_INTENT_ID_ATTRIBUTE
                ).value_text
                assert payment_intent_id == mock_retrieve.return_value['id']
                assert retrieve_addr_resp['status'] == 'requires_confirmation'

    @file_data('fixtures/test_stripe_test_payment_flow.json')
    def test_capture_context_in_progress_payment(
            self,
            confirm_resp,  # pylint: disable=unused-argument
            confirm_resp_in_progress,  # pylint: disable=unused-argument
            create_resp,
            modify_resp,  # pylint: disable=unused-argument
            cancel_resp,
            refund_resp,  # pylint: disable=unused-argument
            retrieve_addr_resp,  # pylint: disable=unused-argument
            retrieve_resp_in_progress):
        """
        Verify that hitting capture-context with a Payment Intent that already exists but it's
        in 'requires_action' state, that a new Payment Intent is created and associated to the basket.
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)
        payment_intent_id = retrieve_resp_in_progress['id']
        basket_add_payment_intent_id_attribute(basket, payment_intent_id)

        with mock.patch('stripe.PaymentIntent.retrieve') as mock_retrieve:
            mock_retrieve.return_value = retrieve_resp_in_progress
            # If Payment Intent gets into 'requires_action' status without confirmation from the BNPL,
            # we create a new Payment Intent for retry payment in the MFE
            with mock.patch('stripe.PaymentIntent.cancel') as mock_cancel:
                mock_cancel.return_value = cancel_resp
                with mock.patch('stripe.PaymentIntent.create') as mock_create:
                    mock_create.return_value = create_resp
                    response = self.client.get(self.capture_context_url)

                    # Basket should have the new Payment Intent ID
                    mock_create.assert_called_once()
                    mock_cancel.assert_called_once()
                    payment_intent_id = BasketAttribute.objects.get(
                        basket=basket,
                        attribute_type__name=PAYMENT_INTENT_ID_ATTRIBUTE
                    ).value_text
                    assert payment_intent_id == mock_create.return_value['id']

                    # Response should have the same order_number and new client secret
                    assert response.json() == {
                        'capture_context': {
                            'key_id': mock_create.return_value['client_secret'],
                            'order_id': basket.order_number,
                        }
                    }

    def test_payment_error_no_basket(self):
        """
        Verify view redirects to error page if no basket exists for payment_intent_id.
        """
        # Post without actually making a basket
        response = self.client.post(
            self.stripe_checkout_url,
            data={
                'payment_intent_id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                'skus': '',
                'dynamic_payment_methods_enabled': 'false',
            },
        )
        assert response.status_code == 302
        assert response.url == "http://testserver.fake/checkout/error/"

    def test_payment_error_sku_mismatch(self):
        """
        Verify a sku mismatch between basket and request logs warning.
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)

        with self.assertLogs(level='WARNING') as log:
            response = self.payment_flow_with_mocked_stripe_calls(
                self.stripe_checkout_url,
                {
                    'payment_intent_id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                    'skus': 'totally_the_wrong_sku',
                    'dynamic_payment_methods_enabled': 'false',
                },
            )
            assert response.json() == {'sku_error': True}
            assert response.status_code == 400
            expected_log = (
                "WARNING:ecommerce.extensions.payment.views.stripe:"
                "Basket [%s] SKU mismatch! request_skus "
                "[{'totally_the_wrong_sku'}] and basket_skus [{'%s'}]."
                % (basket.id, basket.lines.first().stockrecord.partner_sku)
            )
            actual_log = log.output[0]
            assert actual_log == expected_log

    def test_payment_check_sdn_returns_hits(self):
        """
        Verify positive SDN hits returns correct error JSON.
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)

        with mock.patch('ecommerce.extensions.payment.views.stripe.checkSDN') as mock_sdn_check:
            mock_sdn_check.return_value = 1
            response = self.payment_flow_with_mocked_stripe_calls(
                self.stripe_checkout_url,
                {
                    'payment_intent_id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                    'skus': basket.lines.first().stockrecord.partner_sku,
                    'dynamic_payment_methods_enabled': 'false',
                },
            )
            assert response.status_code == 400
            assert response.json() == {'sdn_check_failure': {'hit_count': 1}}

    def test_payment_handle_payment_intent_in_progress(self):
        """
        Verify the POST endpoint handles a Payment Intent that is not succeeded yet,
        with a 'requires_action' for a BNPL payment, which will be handled in the MFE.
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)
        payment_intent_id = 'pi_3LsftNIadiFyUl1x2TWxaADZ'
        dynamic_payment_methods_enabled = 'true'

        with self.assertLogs(level='INFO') as log:
            response = self.payment_flow_with_mocked_stripe_calls(
                self.stripe_checkout_url,
                {
                    'payment_intent_id': payment_intent_id,
                    'skus': basket.lines.first().stockrecord.partner_sku,
                    'dynamic_payment_methods_enabled': dynamic_payment_methods_enabled,
                },
                in_progress_payment=True,
            )

            assert response.status_code == 201
            # Should return 'requires_action' to the MFE with the same Payment Intent
            assert response.json()['status'] == 'requires_action'
            assert response.json()['transaction_id'] == payment_intent_id
            expected_log = (
                'INFO:ecommerce.extensions.payment.processors.stripe:'
                'Confirmed Stripe payment intent [{}] for basket [{}] and order number [{}], '
                'with dynamic_payment_methods_enabled [{}] and status [{}].'.format(
                    payment_intent_id,
                    basket.id,
                    basket.order_number,
                    dynamic_payment_methods_enabled,
                    response.json()['status']
                )
            )
            actual_log = log.output[6]
            assert actual_log == expected_log

    def test_handle_payment_fails_with_carderror(self):
        """
        Verify handle payment failing with CardError returns correct error JSON.
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)

        response = self.payment_flow_with_mocked_stripe_calls(
            self.stripe_checkout_url,
            {
                'payment_intent_id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                'skus': basket.lines.first().stockrecord.partner_sku,
                'dynamic_payment_methods_enabled': 'false',
            },
            confirm_side_effect=stripe.error.CardError('Oops!', {}, 'card_declined'),
        )
        assert response.status_code == 400
        assert response.json() == {'error_code': 'card_declined', 'user_message': 'Oops!'}

    def test_handle_payment_fails_with_unexpected_error(self):
        """
        Verify handle payment failing with unexpected error returns correct JSON response.
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)

        path = 'ecommerce.extensions.payment.views.stripe.StripeCheckoutView.handle_payment'
        with mock.patch(path) as mock_handle_payment:
            mock_handle_payment.side_effect = ZeroDivisionError
            response = self.payment_flow_with_mocked_stripe_calls(
                self.stripe_checkout_url,
                {
                    'payment_intent_id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                    'skus': basket.lines.first().stockrecord.partner_sku,
                    'dynamic_payment_methods_enabled': 'false',
                },
            )
            assert response.status_code == 400
            assert response.json() == {}

    def test_create_billing_address_fails(self):
        """
        Verify order is not successful if billing address objects fails
        to be created.
        """
        basket = self.create_basket(product_class=SEAT_PRODUCT_CLASS_NAME)

        path = 'ecommerce.extensions.payment.views.stripe.StripeCheckoutView.create_billing_address'
        with mock.patch(path) as mock_billing_create:
            mock_billing_create.side_effect = Exception
            response = self.payment_flow_with_mocked_stripe_calls(
                self.stripe_checkout_url,
                {
                    'payment_intent_id': 'pi_3LsftNIadiFyUl1x2TWxaADZ',
                    'skus': basket.lines.first().stockrecord.partner_sku,
                },
            )
            assert response.status_code == 400
            assert response.json() == {}

        basket.refresh_from_db()
        assert not basket.order_set.exists()

    # def test_successful_payment_for_bulk_purchase(self):
    #     """
    #     Verify that when a Order has been successfully placed for bulk
    #     purchase then that order is linked to the provided business client.
    #     """
    #     toggle_switch(ENROLLMENT_CODE_SWITCH, True)

    #     course = CourseFactory(partner=self.partner)
    #     course.create_or_update_seat('verified', True, 50, create_enrollment_code=True)
    #     basket = create_basket(owner=self.user, site=self.site)
    #     enrollment_code = Product.objects.get(product_class__name=ENROLLMENT_CODE_PRODUCT_CLASS_NAME)
    #     basket.add_product(enrollment_code, quantity=1)
    #     basket.strategy = Selector().strategy()

    #     data = self.generate_form_data(basket.id)
    #     data.update({'organization': 'Dummy Business Client'})
    #     data.update({PURCHASER_BEHALF_ATTRIBUTE: 'False'})

    #     # Manually add organization attribute on the basket for testing
    #     basket_add_organization_attribute(basket, data)

    #     card_type = 'visa'
    #     label = '4242'
    #     payment_intent = stripe.PaymentIntent.construct_from({
    #         'id': 'pi_testtesttest',
    #         'source': {
    #             'brand': card_type,
    #             'last4': label,
    #         },
    #     }, 'fake-key')

    #     billing_address = BillingAddressFactory()
    #     with mock.patch.object(Stripe, 'get_address_from_token') as address_mock:
    #         address_mock.return_value = billing_address

    #         with mock.patch.object(stripe.PaymentIntent, 'create') as pi_mock:
    #             pi_mock.return_value = payment_intent
    #             response = self.client.post(self.path, data)

    #         address_mock.assert_called_once_with(data['payment_intent_id'])

    #     self.assert_successful_order_response(response, basket.order_number)
    #     self.assert_order_created(basket, billing_address, card_type, label)

    #     # Now verify that a new business client has been created and current
    #     # order is now linked with that client through Invoice model.
    #     order = Order.objects.filter(basket=basket).first()
    #     business_client = BusinessClient.objects.get(name=data['organization'])
    #     assert Invoice.objects.get(order=order).business_client == business_client
