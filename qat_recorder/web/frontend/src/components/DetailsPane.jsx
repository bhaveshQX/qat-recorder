export default function DetailsPane({ content }) {
  return (
    <>
      <div className="details-header">Details</div>
      <pre className="details-body">{content || 'Select a step to see how it was identified'}</pre>
    </>
  );
}
