from .company import current_membership


def navigation_context(request):
    membership = current_membership(request)
    return {
        "current_membership": membership,
        "current_company": membership.company if membership else None,
    }
