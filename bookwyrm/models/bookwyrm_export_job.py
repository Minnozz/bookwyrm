"""Export user account to tar.gz file for import into another Bookwyrm instance"""

import logging
from uuid import uuid4

from django.db.models import FileField
from django.db.models import Q
from django.core.serializers.json import DjangoJSONEncoder
from django.core.files.base import ContentFile

from bookwyrm.models import AnnualGoal, ReadThrough, ShelfBook, Shelf, List, ListItem
from bookwyrm.models import Review, Comment, Quotation
from bookwyrm.models import Edition, Book
from bookwyrm.models import UserFollows, User, UserBlocks
from bookwyrm.models.job import ParentJob, ParentTask
from bookwyrm.settings import DOMAIN
from bookwyrm.tasks import app, IMPORTS
from bookwyrm.utils.tar import BookwyrmTarFile

logger = logging.getLogger(__name__)


class BookwyrmExportJob(ParentJob):
    """entry for a specific request to export a bookwyrm user"""

    export_data = FileField(null=True)

    def start_job(self):
        """Start the job"""
        start_export_task.delay(job_id=self.id, no_children=True)

        return self


@app.task(queue=IMPORTS, base=ParentTask)
def start_export_task(**kwargs):
    """trigger the child tasks for each row"""
    job = BookwyrmExportJob.objects.get(id=kwargs["job_id"])

    # don't start the job if it was stopped from the UI
    if job.complete:
        return
    try:
        # This is where ChildJobs get made
        job.export_data = ContentFile(b"", str(uuid4()))
        json_data = json_export(job.user)
        tar_export(json_data, job.user, job.export_data)
        job.save(update_fields=["export_data"])
    except Exception as err:  # pylint: disable=broad-except
        logger.exception("User Export Job %s Failed with error: %s", job.id, err)
        job.set_status("failed")

    job.set_status("complete")


def tar_export(json_data: str, user, file):
    """wrap the export information in a tar file"""
    file.open("wb")
    with BookwyrmTarFile.open(mode="w:gz", fileobj=file) as tar:
        tar.write_bytes(json_data.encode("utf-8"))

        # Add avatar image if present
        if getattr(user, "avatar", False):
            tar.add_image(user.avatar, filename="avatar")

        editions, books = get_books_for_user(user)  # pylint: disable=unused-variable
        for book in editions:
            if getattr(book, "cover", False):
                tar.add_image(book.cover)

    file.close()


def json_export(user):  # pylint: disable=too-many-locals, too-many-statements
    """Generate an export for a user"""
    # user
    exported_user = {}
    vals = [
        "username",
        "name",
        "summary",
        "manually_approves_followers",
        "hide_follows",
        "show_goal",
        "show_suggested_users",
        "discoverable",
        "preferred_timezone",
        "default_post_privacy",
    ]
    for k in vals:
        exported_user[k] = getattr(user, k)

    if getattr(user, "avatar", False):
        exported_user["avatar"] = f'https://{DOMAIN}{getattr(user, "avatar").url}'

    # reading goals
    reading_goals = AnnualGoal.objects.filter(user=user).distinct()
    goals_list = []
    # TODO: either error checking should be more sophisticated
    # or maybe we don't need this try/except
    try:
        for goal in reading_goals:
            goals_list.append(
                {"goal": goal.goal, "year": goal.year, "privacy": goal.privacy}
            )
    except Exception:  # pylint: disable=broad-except
        pass

    try:
        readthroughs = ReadThrough.objects.filter(user=user).distinct().values()
        readthroughs = list(readthroughs)
    except Exception:  # pylint: disable=broad-except
        readthroughs = []

    # books
    editions, books = get_books_for_user(user)
    final_books = []

    for book in books.values():
        edition = editions.filter(id=book["id"])
        book["edition"] = edition.values()[0]
        # authors
        book["authors"] = list(edition.first().authors.all().values())
        # readthroughs
        book_readthroughs = (
            ReadThrough.objects.filter(user=user, book=book["id"])
            .distinct()
            .values()
        )
        book["readthroughs"] = list(book_readthroughs)
        # shelves
        shelf_books = ShelfBook.objects.filter(
            user=user, book=book["id"]
        ).distinct()
        shelves_from_books = Shelf.objects.filter(
            shelfbook__in=shelf_books, user=user
        )

        book["shelves"] = list(shelves_from_books.values())
        book["shelf_books"] = {}

        for shelf in shelves_from_books:
            shelf_contents = ShelfBook.objects.filter(
                user=user, shelf=shelf
            ).distinct()

            book["shelf_books"][shelf.identifier] = list(shelf_contents.values())

        # book lists
        book_lists = List.objects.filter(
            books__in=[book["id"]], user=user
        ).distinct()
        book["lists"] = list(book_lists.values())
        book["list_items"] = {}
        for blist in book_lists:
            list_items = ListItem.objects.filter(book_list=blist).distinct()
            book["list_items"][blist.name] = list(list_items.values())

        # reviews
        reviews = Review.objects.filter(user=user, book=book["id"]).distinct()

        book["reviews"] = list(reviews.values())

        # comments
        comments = Comment.objects.filter(user=user, book=book["id"]).distinct()

        book["comments"] = list(comments.values())

        # quotes
        quotes = Quotation.objects.filter(user=user, book=book["id"]).distinct()

        book["quotes"] = list(quotes.values())

        # append everything
        final_books.append(book)

    # saved book lists
    saved_lists = List.objects.filter(id__in=user.saved_lists.all()).distinct()
    saved_lists = [l.remote_id for l in saved_lists]

    # follows
    follows = UserFollows.objects.filter(user_subject=user).distinct()
    following = User.objects.filter(
        userfollows_user_object__in=follows
    ).distinct()
    follows = [f.remote_id for f in following]

    # blocks
    blocks = UserBlocks.objects.filter(user_subject=user).distinct()
    blocking = User.objects.filter(userblocks_user_object__in=blocks).distinct()

    blocks = [b.remote_id for b in blocking]

    data = {
        "user": exported_user,
        "goals": goals_list,
        "books": final_books,
        "saved_lists": saved_lists,
        "follows": follows,
        "blocked_users": blocks,
    }

    return DjangoJSONEncoder().encode(data)


def get_books_for_user(user):
    """Get all the books and editions related to a user
    :returns: tuple of editions, books
    """
    all_books = Edition.viewer_aware_objects(user)
    editions = all_books.filter(
        Q(shelves__user=user)
        | Q(readthrough__user=user)
        | Q(review__user=user)
        | Q(list__user=user)
        | Q(comment__user=user)
        | Q(quotation__user=user)
    ).distinct()
    books = Book.objects.filter(id__in=editions).distinct()
    return editions, books
