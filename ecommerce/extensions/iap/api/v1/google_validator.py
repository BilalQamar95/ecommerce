import logging

from inapppy import GooglePlayVerifier, errors

from ecommerce.extensions.iap.utils import get_consumable_android_sku

logger = logging.getLogger(__name__)


class GooglePlayValidator:
    def validate(self, receipt, configuration, basket):
        """
        Accepts receipt, validates that the purchase has already been completed in
        Google for the mentioned product_id.
        """
        purchase_token = receipt.get('purchase_token')
        # Mobile assumes one course is purchased at a time
        product_sku = get_consumable_android_sku(basket.total_excl_tax)
        verifier = GooglePlayVerifier(
            configuration.get('google_bundle_id'),
            configuration.get('google_service_account_key_file'),
        )
        try:
            result = self.verify_result(verifier, purchase_token, product_sku)
        except errors.GoogleError as exc:
            logger.error('Purchase validation failed %s', exc)
            result = {
                'error': exc.raw_response,
                'message': exc.message
            }

        return result

    def verify_result(self, verifier, purchase_token, product_sku):
        response = verifier.verify_with_result(purchase_token, product_sku, is_subscription=False)
        result = {
            'raw_response': response.raw_response,
            'is_canceled': response.is_canceled,
            'is_expired': response.is_expired
        }
        return result
