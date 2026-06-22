from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from shifts.infrastructure.models import CompanyMembership, Staff


def current_membership(request):
    if not request.user.is_authenticated:
        return None
    return request.user.company_memberships.select_related("company").filter(company__active=True).first()


def current_staff(request, membership=None):
    membership = membership or current_membership(request)
    if not membership:
        return None
    return Staff.objects.filter(company=membership.company, user=request.user, active=True).first()


def admin_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        membership = current_membership(request)
        if not membership or membership.role != CompanyMembership.Role.ADMIN:
            messages.error(request, "管理者権限が必要です。")
            return redirect("home")
        request.membership = membership
        request.company = membership.company
        return view(request, *args, **kwargs)
    return wrapped


def staff_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        membership = current_membership(request)
        staff = current_staff(request, membership)
        if not membership or not staff:
            messages.error(request, "スタッフ情報が紐付いていません。")
            return redirect("home")
        request.membership = membership
        request.company = membership.company
        request.staff = staff
        return view(request, *args, **kwargs)
    return wrapped
