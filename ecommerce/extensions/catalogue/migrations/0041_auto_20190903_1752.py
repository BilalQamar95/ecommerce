# -*- coding: utf-8 -*-
# Generated by Django 1.11.23 on 2019-09-03 17:52
# This is the reverse of ecommerce/extensions/catalogue/migrations/0032_journal_product_class.py
from __future__ import unicode_literals

from django.db import migrations
from oscar.core.loading import get_model
from oscar.core.utils import slugify

JOURNAL_PRODUCT_CLASS_NAME = 'Journal'
Category = get_model("catalogue", "Category")
Product = get_model('catalogue', 'Product')
ProductAttribute = get_model("catalogue", "ProductAttribute")
ProductClass = get_model("catalogue", "ProductClass")
JOURNAL_SLUG_NAME = slugify(JOURNAL_PRODUCT_CLASS_NAME)


def create_product_class(apps, schema_editor):
    """ Create a journal product class """
    for klass in (Category, Product, ProductClass, ProductAttribute):
        klass.skip_history_when_saving = True

    # Create a new product class for journal
    journal = ProductClass(
        track_stock=False,
        requires_shipping=False,
        name=JOURNAL_PRODUCT_CLASS_NAME,
        slug=JOURNAL_SLUG_NAME
    )
    journal.save()

    # Create product attributes for journal products
    pa1 = ProductAttribute.objects.create(
        product_class=journal,
        name="UUID",
        code="UUID",
        type="text",
        required=True
    )
    pa1.save()

    # Create a category for the journal
    Category.add_root(
        description="All journals",
        slug="journals",
        image="",
        name="Journals"
    )


def remove_product_class(apps, schema_editor):
    """ Reverse function. """
    # ProductAttribute is required here for the cascading delete
    for klass in (Category, Product, ProductClass, ProductAttribute):
        klass.skip_history_when_saving = True

    Product.objects.filter(product_class=ProductClass.objects.get(name=JOURNAL_PRODUCT_CLASS_NAME)).delete()
    Category.objects.filter(slug=JOURNAL_SLUG_NAME).delete()
    ProductClass.objects.filter(name=JOURNAL_PRODUCT_CLASS_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalogue', '0040_historicalcategory_historicaloption_historicalproductattribute_historicalproductcategory_historicalp'),
    ]

    operations = [
        migrations.RunPython(remove_product_class, create_product_class),
    ]
