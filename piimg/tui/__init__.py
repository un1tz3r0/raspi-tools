"""piimg's full-screen interface (Textual). See app.py and recipe_editor.py."""
from pathlib import Path


def main(target=None, overlay=None):
    from .app import PiImgApp
    PiImgApp(target, overlay).run()
    return 0


def edit_recipe(recipe=None, image=None):
    """`piimg recipe-edit`: a file path opens (or creates) that file; a bundled
    recipe's name opens a copy of it as a template; nothing shows a picker."""
    from ..frontend.recipe import find_template
    from .recipe_editor import RecipeEditorApp
    if recipe is None:
        RecipeEditorApp(image=image, pick=True).run()
        return 0
    p = Path(recipe)
    template = None if p.exists() else find_template(recipe)
    RecipeEditorApp(None if template else p, image, template).run()
    return 0
