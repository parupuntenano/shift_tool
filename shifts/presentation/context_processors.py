from .company import current_membership
from shifts.infrastructure.models import CompanyMembership, ShiftLeaveRequest


def navigation_context(request):
    membership = current_membership(request)
    pending_leave_request_count = 0
    if membership:
        if membership.role == CompanyMembership.Role.ADMIN:
            pending_leave_request_count = ShiftLeaveRequest.objects.filter(
                period__company=membership.company,
                status=ShiftLeaveRequest.Status.PENDING,
            ).count()
        else:
            pending_leave_request_count = ShiftLeaveRequest.objects.filter(
                staff__company=membership.company,
                staff__user=request.user,
                status=ShiftLeaveRequest.Status.PENDING,
            ).count()
    return {
        "current_membership": membership,
        "current_company": membership.company if membership else None,
        "pending_leave_request_count": pending_leave_request_count,
    }
