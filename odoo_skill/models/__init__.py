"""Model operation modules for Odoo."""

from .partner import PartnerOps
from .sale_order import SaleOrderOps
from .invoice import InvoiceOps
from .inventory import InventoryOps
from .crm import CRMOps
from .purchase import PurchaseOrderOps
from .project import ProjectOps
from .hr import HROps
from .manufacturing import ManufacturingOps
from .calendar_ops import CalendarOps
from .fleet import FleetOps
from .ecommerce import EcommerceOps
from ._base import (
    BaseOps,
    OdooActionNotAllowedError,
    OdooModuleNotInstalledError,
)
from .todo_matrix import TodoMatrixOps
from .repair import RepairOps
from .rma import RMAOps
from .warranty import WarrantyOps
from .consignment import ConsignmentOps
from .helpdesk import HelpdeskOps
from .messaging import MessagingOps
from .field_service import FieldServiceOps
from .ebay_listing import EbayListingOps
from .product_gui import ProductGuiOps
from .itad import ITADOps
from .fb_marketplace import FbMarketplaceOps
from .inbound import InboundOps
from .order_status import OrderStatusOps
from .ebay_messages import EbayMessageOps
from .auction import AuctionOps
from .photography import PhotographyOps
from .pc_build import PcBuildOps

__all__ = [
    "PartnerOps",
    "SaleOrderOps",
    "InvoiceOps",
    "InventoryOps",
    "CRMOps",
    "PurchaseOrderOps",
    "ProjectOps",
    "HROps",
    "ManufacturingOps",
    "CalendarOps",
    "FleetOps",
    "EcommerceOps",
    "TodoMatrixOps",
    "BaseOps",
    "OdooModuleNotInstalledError",
    "OdooActionNotAllowedError",
    "RepairOps",
    "RMAOps",
    "WarrantyOps",
    "ConsignmentOps",
    "HelpdeskOps",
    "MessagingOps",
    "FieldServiceOps",
    "EbayListingOps",
    "ProductGuiOps",
    "ITADOps",
    "FbMarketplaceOps",
    "InboundOps",
    "OrderStatusOps",
    "EbayMessageOps",
    "AuctionOps",
    "PhotographyOps",
    "PcBuildOps",
]
