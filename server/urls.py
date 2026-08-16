"""Root URLconf.

Empty by design at phase 0. The decision endpoint arrives in phase 2 and the
management API in phase 5, and they are mounted from separate settings modules
so a decision worker never routes a management request (D7).
"""

urlpatterns: list = []
